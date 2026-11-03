import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from pydantic import ValidationError

from sessionscribe.bot import (
    BotPolicy,
    ControlBot,
    ControlService,
    DiscordDelivery,
    authenticated_actor,
    resolved_channel,
)
from sessionscribe.commands import definitions as session_command_definitions
from sessionscribe.control import Denied
from sessionscribe.credentials import CredentialError, discord_token
from sessionscribe.guilds import AdminActor, GuildChannel, Setup
from sessionscribe.health import control_ready
from sessionscribe.voice import ReceiveUnavailable


@pytest.fixture
def policy(tmp_path):
    raw = json.loads(Path("config/bot.example.json").read_text())
    raw["data_root"] = str(tmp_path / "data")
    raw["token_file"] = str(tmp_path / "bot.env")
    return BotPolicy.model_validate_json(json.dumps(raw))


def admin(guild="1000", permissions=32):
    return AdminActor(guild_id=guild, channel_id="10", user_id="20", permissions=permissions)


def setup(guild="1000"):
    return Setup(
        mode="meeting",
        command_channel=GuildChannel(id="10", guild_id=guild, kind="text"),
        voice_channel=GuildChannel(id="11", guild_id=guild, kind="voice"),
        output_channel=GuildChannel(id="12", guild_id=guild, kind="text"),
    )


def test_private_credential_parser(tmp_path):
    path = tmp_path / "bot.env"
    synthetic = "synthetic.token.for-tests-only"
    path.write_text(f"# private file\nDISCORD_TOKEN='{synthetic}'\n")
    path.chmod(0o600)
    secret = discord_token(path)
    assert secret.get_secret_value() == synthetic
    assert synthetic not in repr(secret)
    for mode in (0o644, 0o640):
        path.chmod(mode)
        with pytest.raises(CredentialError):
            discord_token(path)
    path.chmod(0o600)
    for raw in (
        "DISCORD_TOKEN=",
        "DISCORD_TOKEN=$(host-command)",
        "DISCORD_TOKEN=synthetic-value-for-testing\nOTHER=value",
    ):
        path.write_text(raw)
        with pytest.raises(CredentialError) as error:
            discord_token(path)
        assert raw not in str(error.value)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(CredentialError):
        discord_token(alias)
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CredentialError):
        discord_token(parent_alias / "bot.env")
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(CredentialError):
        discord_token(directory)


def test_operator_gates_cannot_be_enabled_by_policy(policy):
    for key in ("voice_reception", "cloud_processing"):
        with pytest.raises(ValidationError):
            BotPolicy.model_validate(policy.model_dump() | {key: "enabled"})


def test_control_policy_capacity_roles_guilds_and_voice_gate(policy):
    service = ControlService(policy.model_copy(update={"max_guilds": 1}), Mock(), Mock())
    try:
        with pytest.raises(Denied):
            service.configure("setup", admin(permissions=0), setup().model_dump())
        configured = service.configure("setup", admin(), setup().model_dump())
        assert configured.guild_id == "1000" and not configured.recording_enabled
        with pytest.raises(Denied):
            service.configure("setup", admin("2000"), setup("2000").model_dump())
        # A configuration's recording flag cannot unlock the missing adapter.
        service.directory.db.execute(
            "UPDATE configurations SET body=? WHERE guild_id=?",
            (configured.model_copy(update={"recording_enabled": True}).model_dump_json(), "1000"),
        )
        with pytest.raises(ReceiveUnavailable):
            service.session("start", admin(), {"voice_channel_id": "11"})
        with pytest.raises(ReceiveUnavailable):
            service.session("startsession", admin(), {"voice_channel_id": "11"})
        assert service.session("status", admin(), {})["sessions"] == []
        with pytest.raises(KeyError):
            service.session("status", admin("2000"), {})
        with pytest.raises(ValidationError):
            service.session("delete", admin(), {"session_id": "../../escape"})
    finally:
        service.close()


def test_alias_command_is_self_bound_and_session_consent_is_not_reused(policy):
    statuses = []
    service = ControlService(
        policy,
        lambda guild, channel, message: statuses.append((guild, channel, message))
        or "synthetic-status",
        Mock(),
    )
    owner = admin()
    voice_actor = owner.model_copy(update={"voice_channel_id": "11"})
    try:
        service.configure("setup", owner, setup().model_dump())
        profile = service.session(
            "alias",
            voice_actor,
            {"alias": "Ada"},
            current_participants=frozenset({owner.user_id}),
        )
        assert profile == {"alias_saved": True, "consent_applied": False}
        assert service.directory.voice_aliases("1000", "11") == {owner.user_id: "Ada"}

        config, revision = service.directory.configuration("1000")
        service.directory._save(
            owner,
            config.model_copy(
                update={
                    "recording_enabled": True,
                    "command_channel_ids": config.command_channel_ids | {"13"},
                }
            ),
            revision,
        )
        with service.directory.controller(
            owner, lambda *_: "synthetic-status", Mock()
        ) as controller:
            session = controller.start(
                owner,
                "11",
                frozenset({owner.user_id}),
                speaker_aliases=service.directory.voice_aliases("1000", "11"),
            )
        assert session.consented == frozenset()
        assert service.session_voice_channel(owner, session.id) == "11"

        alias_command = next(
            command for command in session_command_definitions() if command["name"] == "alias"
        )
        consent_option = next(
            option for option in alias_command["options"] if option["name"] == "recording_consent"
        )
        assert consent_option["required"] is False

        other_command_channel = voice_actor.model_copy(update={"channel_id": "13"})
        with pytest.raises(Denied):
            service.session(
                "alias",
                other_command_channel,
                {"alias": "Changed", "recording_consent": "accept"},
                current_participants=frozenset({owner.user_id}),
            )
        assert service.directory.voice_aliases("1000", "11") == {owner.user_id: "Ada"}

        with pytest.raises(ValueError, match="recording consent choice required"):
            service.session(
                "alias",
                voice_actor,
                {"alias": None},
                current_participants=frozenset({owner.user_id}),
            )

        applied = service.session(
            "alias",
            voice_actor,
            {"alias": None, "recording_consent": "accept"},
            current_participants=frozenset({owner.user_id}),
        )
        assert applied["consent_applied"] is True
        store = service.directory.store("1000")
        try:
            session = store.session(session.id)
            assert session.speaker_aliases == {owner.user_id: "Ada"}
            assert session.consented == frozenset({owner.user_id})
        finally:
            store.close()

        newcomer = voice_actor.model_copy(update={"user_id": "21"})
        assert service.voice_membership("1000", "11", frozenset({"20", "21"}))
        paused = service.session(
            "alias",
            newcomer,
            {"alias": "Ben", "recording_consent": "accept"},
            current_participants=frozenset({"20", "21"}),
        )
        assert paused["state"] == "paused"
        store = service.directory.store("1000")
        try:
            session = store.session(session.id)
            assert session.consented == frozenset({"20", "21"})
            assert session.speaker_aliases == {"20": "Ada", "21": "Ben"}
        finally:
            store.close()

        assert service.session("alias-delete", voice_actor, {}) is True
        store = service.directory.store("1000")
        try:
            session = store.session(session.id)
            assert "20" not in session.speaker_aliases
            assert "20" not in session.consented
            assert session.stop_reason == "consent_required"
        finally:
            store.close()
        assert statuses
    finally:
        service.close()


def test_operator_guild_allowlist(policy):
    service = ControlService(
        policy.model_copy(update={"allowed_guild_ids": frozenset({"3000"})}), Mock(), Mock()
    )
    try:
        with pytest.raises(Denied):
            service.configure("setup", admin(), setup().model_dump())
    finally:
        service.close()


def channel(guild, id=12, *, attach=True):
    result = Mock(spec=discord.TextChannel)
    result.id, result.guild = id, guild
    result.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True, attach_files=attach
    )
    result.send = AsyncMock(return_value=SimpleNamespace(id=1234))
    return result


def context(permissions=32):
    guild = SimpleNamespace(id=1000, me=object())
    author = Mock(spec=discord.Member)
    author.guild, author.id, author.roles = guild, 20, []
    author.voice = None
    author.guild_permissions = discord.Permissions(permissions)
    text = channel(guild, 10)
    voice = Mock(spec=discord.VoiceChannel)
    voice.id, voice.guild = 11, guild
    voice.members = []
    voice.permissions_for.return_value = discord.Permissions(view_channel=True, connect=True)
    output = channel(guild)
    channels = {10: text, 11: voice, 12: output}
    guild.get_channel = channels.get
    return SimpleNamespace(
        guild=guild, author=author, channel=text, defer=AsyncMock(), respond=AsyncMock()
    )


def test_interaction_and_resolved_channel_boundary():
    ctx = context()
    actor = authenticated_actor(ctx)
    assert actor.guild_id == "1000" and actor.permissions == 32
    assert resolved_channel(ctx.guild, SimpleNamespace(id=12), "text").id == "12"
    for wrong in (SimpleNamespace(id=999), SimpleNamespace(id=11)):
        with pytest.raises(Denied):
            resolved_channel(ctx.guild, wrong, "text")
    ctx.guild.get_channel(12).permissions_for.return_value.attach_files = False
    with pytest.raises(Denied):
        resolved_channel(ctx.guild, SimpleNamespace(id=12), "text")
    assert resolved_channel(ctx.guild, SimpleNamespace(id=12), "text", attachments=False).id == "12"
    ctx.author = SimpleNamespace(
        id=20, guild=ctx.guild, guild_permissions=discord.Permissions.all()
    )
    with pytest.raises(Denied):
        authenticated_actor(ctx)


def test_real_pycord_registration_and_callbacks(policy):
    async def scenario():
        bot = ControlBot(policy)
        bot.loop = asyncio.get_running_loop()
        await bot.policy_call(
            lambda: setattr(bot, "service", ControlService(policy, Mock(), Mock()))
        )
        try:
            commands = {cmd.name: cmd for cmd in bot.client.pending_application_commands}
            assert set(commands) == {
                "setup",
                "config",
                "start",
                "startsession",
                "alias",
                "alias-delete",
                "consent",
                "revoke",
                "pause",
                "pausesession",
                "resume",
                "stop",
                "stopsession",
                "status",
                "queue",
                "process",
                "result",
                "delete",
            }
            assert all(cmd.to_dict()["contexts"] == [0] for cmd in commands.values())
            assert bot.client.intents.voice_states
            assert not bot.client.intents.members and not bot.client.intents.message_content
            ctx = context(permissions=0)
            await commands["setup"].callback(ctx, **setup().model_dump())
            assert "Berechtigung" in ctx.respond.call_args.args[0]
            ctx = context()
            await commands["setup"].callback(
                ctx,
                mode="meeting",
                command_channel=SimpleNamespace(id=10),
                voice_channel=SimpleNamespace(id=11),
                output_channel=SimpleNamespace(id=12),
            )
            assert ctx.defer.call_args.kwargs["ephemeral"]
            children = {cmd.name: cmd for cmd in commands["config"].subcommands}
            for child in commands["config"].to_dict()["options"]:
                assert child["type"] == 1 and "contexts" not in child
            await children["protocol"].callback(
                ctx, voice_channel=SimpleNamespace(id=11), channel=None, inherit=True
            )
            await commands["start"].callback(ctx, voice_channel=SimpleNamespace(id=11))
            assert "DAVE" in ctx.respond.call_args.args[0]
            await commands["startsession"].callback(ctx, voice_channel=SimpleNamespace(id=11))
            assert "DAVE" in ctx.respond.call_args.args[0]
            await commands["status"].callback(ctx)
            assert '"sessions": []' in ctx.respond.call_args.args[0]
            bot.health()
            health = json.loads((policy.data_root / "control-health.json").read_text())
            assert health["voice_ready"] is False and health["revision"] == "development"
            assert "1000" not in json.dumps(health)
        finally:
            await bot.policy_call(bot.service.close)
            bot.executor.shutdown()
            await bot.client.close()

    asyncio.run(scenario())


def test_discord_delivery_rechecks_guild_permissions_and_mentions():
    async def scenario():
        ctx = context()
        client = SimpleNamespace(get_guild=lambda id: ctx.guild if id == ctx.guild.id else None)
        delivery = DiscordDelivery(client, asyncio.get_running_loop(), "1000")
        assert await delivery._send("12", content="@everyone") == "1234"
        kwargs = ctx.guild.get_channel(12).send.call_args.kwargs
        assert kwargs["allowed_mentions"].to_dict()["parse"] == []
        assert kwargs["suppress_embeds"] is True
        with pytest.raises(Denied):
            await delivery._send("99", content="denied")
        with pytest.raises(Denied):
            await DiscordDelivery(client, asyncio.get_running_loop(), "2000")._send("12", "no")
        with pytest.raises(Denied):
            await delivery._send("12", filename="../../secret", data=b"synthetic")
        ctx.guild.get_channel(12).permissions_for.return_value.attach_files = False
        with pytest.raises(Denied):
            await delivery._send("12", filename="protocol-" + "a" * 32 + ".md", data=b"text")
        with pytest.raises(Denied):
            delivery.send("12", "text", allowed_mentions={})

    asyncio.run(scenario())


def test_readiness_requires_registered_commands_fresh_revision(policy, caplog):
    async def scenario():
        bot = ControlBot(policy)
        bot.client.sync_commands = AsyncMock(side_effect=RuntimeError("synthetic-private-detail"))
        try:
            await bot.on_ready()
            assert not bot.ready and not control_ready(policy.data_root, "development")
            assert "synthetic-private-detail" not in caplog.text
            bot.client.sync_commands = AsyncMock()
            bot.client.http.get_global_commands = AsyncMock(
                return_value=[{"name": cmd.name} for cmd in bot.client.pending_application_commands]
            )
            await bot.on_ready()
            assert bot.ready and control_ready(policy.data_root, "development")
            assert not control_ready(policy.data_root, "a" * 40)
            health = json.loads((policy.data_root / "control-health.json").read_text())
            assert not control_ready(policy.data_root, "development", now=health["checked_at"] + 66)
            assert not control_ready(policy.data_root, "development", now=health["checked_at"] - 1)
            await bot.on_disconnect()
            assert not control_ready(policy.data_root, "development")
            (policy.data_root / "control-health.json").write_text('{"checked_at": NaN}')
            assert not control_ready(policy.data_root, "development")
        finally:
            bot.executor.shutdown()
            await bot.client.close()

    asyncio.run(scenario())


def test_bot_cancellation_closes_transport_database_and_readiness(policy):
    policy.token_file.write_text("DISCORD_TOKEN=synthetic.token.for-tests-only\n")
    policy.token_file.chmod(0o600)

    async def scenario():
        bot = ControlBot(policy)
        started = asyncio.Event()

        async def start(token):
            assert token == "synthetic.token.for-tests-only"
            started.set()
            await asyncio.Event().wait()

        bot.client.start = start
        bot.client.close = AsyncMock()
        task = asyncio.create_task(bot.run())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        bot.client.close.assert_awaited_once()
        assert not control_ready(policy.data_root, "development")

    asyncio.run(scenario())


def test_command_flood_is_bounded_before_executor_submission(policy):
    async def scenario():
        bot = ControlBot(policy)
        try:
            for _ in range(16):
                await bot.command_slots.acquire()
            ctx = context()
            await bot.invoke(ctx, "status", {}, config=False)
            assert "Zu viele" in ctx.respond.call_args.args[0]
            ctx.defer.assert_not_awaited()
        finally:
            bot.executor.shutdown()
            await bot.client.close()

    asyncio.run(scenario())
