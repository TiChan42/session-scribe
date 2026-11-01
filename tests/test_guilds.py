import pytest

from sessionscribe.control import Denied
from sessionscribe.guilds import (
    AdminActor,
    GuildChannel,
    GuildCommands,
    GuildDirectory,
    GuildRole,
    Setup,
    definitions,
)
from sessionscribe.models import InferenceSettings
from sessionscribe.store import Conflict


def actor(guild="1000", *, permissions=32):
    return AdminActor(
        guild_id=guild, channel_id=guild + "2", user_id="1001", permissions=permissions
    )


def options(guild="1000", mode="meeting"):
    return Setup(
        mode=mode,
        command_channel=GuildChannel(id=guild + "2", guild_id=guild, kind="text"),
        voice_channel=GuildChannel(id=guild + "3", guild_id=guild, kind="voice"),
        output_channel=GuildChannel(id=guild + "4", guild_id=guild, kind="text"),
    )


@pytest.fixture
def directory(tmp_path):
    d = GuildDirectory(
        tmp_path / "bot",
        InferenceSettings(model="synthetic-fixture", context_tokens=32768, output_tokens=2048),
    )
    yield d
    d.close()


def test_one_directory_configures_many_guilds_with_meeting_and_dnd(directory):
    commands = GuildCommands(directory)
    first = commands.setup(actor(), options())
    second = commands.setup(actor("2000"), options("2000", "dnd"))
    assert first.tenant == "1000" and first.mode == "meeting"
    assert second.tenant == "2000" and second.mode == "dnd"
    assert not first.cloud_enabled and not first.recording_enabled
    assert commands.show(actor()).output_channel_id == "10004"
    assert commands.show(actor("2000")).output_channel_id == "20004"
    assert "DISCORD_TOKEN" not in first.model_dump_json()


@pytest.mark.parametrize("permissions", [0, 1, 16])
def test_setup_requires_real_manage_guild_permission(directory, permissions):
    with pytest.raises(Denied):
        directory.setup(actor(permissions=permissions), options())


def test_foreign_channels_and_roles_cannot_be_configured(directory):
    cfg = options().model_copy(
        update={"output_channel": GuildChannel(id="20004", guild_id="2000", kind="text")}
    )
    with pytest.raises(Denied):
        directory.setup(actor(), cfg)
    with pytest.raises(Denied):
        directory.setup(
            actor(),
            options().model_copy(
                update={"operator_roles": (GuildRole(id="2010", guild_id="2000"),)}
            ),
        )
    with pytest.raises(KeyError):
        directory.configuration("1000")


def test_guild_configuration_survives_reopen_and_mode_changes_are_local(directory):
    directory.setup(actor(), options())
    directory.setup(actor("2000"), options("2000"))
    root, inference = directory.files.root, directory.inference
    directory.set_mode(actor(), "dnd")
    another = GuildDirectory(root, inference)
    try:
        assert another.show(actor()).mode == "dnd"
        assert another.show(actor("2000")).mode == "meeting"
    finally:
        another.close()


def test_same_mode_in_two_guilds_never_shares_sessions_or_budgets(directory):
    for gid in ("1000", "2000"):
        directory.setup(actor(gid), options(gid))
    config, rev = directory.configuration("1000")
    directory._save(actor(), config.model_copy(update={"recording_enabled": True}), rev)
    with directory.controller(
        actor(), lambda *_: "synthetic-status", lambda _sid: None
    ) as controller:
        session = controller.start(actor(), "10003", frozenset({"1001"}))
        assert session.mode == "meeting" and session.tenant == "1000"
    with directory.controller(
        actor("2000"), lambda *_: "synthetic-status", lambda _sid: None
    ) as other:
        with pytest.raises(KeyError):
            other.store.session(session.id)
        assert other.config.mode == "meeting"
    first, second = directory.store("1000"), directory.store("2000")
    try:
        first.reserve_budget("synthetic-day", 100, max_calls=1, max_tokens=100)
        assert second.db.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 0
        assert first.files.root != second.files.root
    finally:
        first.close()
        second.close()


def test_active_session_prevents_reconfiguration(directory):
    directory.setup(actor(), options())
    config, revision = directory.configuration("1000")
    directory._save(actor(), config.model_copy(update={"recording_enabled": True}), revision)
    with directory.controller(actor(), lambda *_: "synthetic-status", lambda _sid: None) as control:
        session = control.start(actor(), "10003", frozenset({"1001"}))
    with pytest.raises(Conflict):
        directory.set_mode(actor(), "dnd")
    with directory.controller(actor(), lambda *_: "synthetic-status", lambda _sid: None) as control:
        control.stop(actor(), session.id)
    assert directory.set_mode(actor(), "dnd").mode == "dnd"


def test_configuration_lock_prevents_mid_action_change(directory):
    directory.setup(actor(), options())
    with (
        directory.controller(actor(), lambda *_: "synthetic-status", lambda _sid: None),
        pytest.raises(Conflict),
    ):
        directory.set_mode(actor(), "dnd")


def test_config_role_command_checks_guild_and_uses_numeric_ids(directory):
    directory.setup(actor(), options())
    updated = GuildCommands(directory).roles(
        actor(), "start", (GuildRole(id="1010", guild_id="1000"),)
    )
    assert updated.grants["start"].roles == frozenset({"1010"})
    with pytest.raises(Denied):
        directory.set_roles(actor(), "start", (GuildRole(id="2010", guild_id="2000"),))


def test_slash_setup_offers_only_meeting_and_dnd():
    setup, config = definitions()
    assert setup["options"][0]["choices"] == [
        {"name": "Meeting", "value": "meeting"},
        {"name": "DnD", "value": "dnd"},
    ]
    assert setup["default_member_permissions"] == "32"
    assert not config["dm_permission"]


def test_protocol_routes_override_inherit_or_disable_default(directory):
    config = directory.setup(actor(), options())
    assert config.protocol_channel("10003") == "10004"
    commands = GuildCommands(directory)
    default = GuildChannel(id="10009", guild_id="1000", kind="text")
    voice = GuildChannel(id="10006", guild_id="1000", kind="voice")
    explicit = GuildChannel(id="10007", guild_id="1000", kind="text")
    cfg = commands.default_protocol(actor(), default)
    assert cfg.protocol_channel("10003") == "10009"
    cfg = commands.protocol(actor(), voice, explicit)
    assert cfg.protocol_channel("10006") == "10007"
    assert cfg.protocol_channel("10003") == "10009"
    cfg = commands.protocol(actor(), voice, None)
    assert cfg.protocol_channel("10006") is None
    cfg = commands.protocol(actor(), voice, None, inherit=True)
    assert cfg.protocol_channel("10006") == "10009"
    cfg = commands.default_protocol(actor(), None)
    assert cfg.protocol_channel("10003") is None and cfg.protocol_channel("10006") is None


def test_foreign_protocol_targets_are_denied_and_other_guilds_are_unchanged(directory):
    directory.setup(actor(), options())
    directory.setup(actor("2000"), options("2000"))
    with pytest.raises(Denied):
        directory.set_default_protocol(
            actor(), GuildChannel(id="20004", guild_id="2000", kind="text")
        )
    with pytest.raises(Denied):
        directory.set_voice_protocol(
            actor(),
            GuildChannel(id="10003", guild_id="1000", kind="voice"),
            GuildChannel(id="20004", guild_id="2000", kind="text"),
        )
    directory.set_default_protocol(actor(), None)
    assert directory.show(actor("2000")).default_protocol_channel_id == "20004"


def test_protocol_route_persists_and_unknown_voice_key_is_rejected(directory):
    from pydantic import ValidationError

    directory.setup(actor(), options())
    cfg = directory.set_voice_protocol(
        actor(),
        GuildChannel(id="10006", guild_id="1000", kind="voice"),
        GuildChannel(id="10007", guild_id="1000", kind="text"),
    )
    another = GuildDirectory(directory.files.root, directory.inference)
    try:
        assert another.show(actor()).protocol_channel("10006") == "10007"
    finally:
        another.close()
    with pytest.raises(ValidationError):
        type(cfg).model_validate(cfg.model_dump() | {"voice_protocol_channels": {"9999": "10004"}})


def test_recording_bot_allowlist_is_operator_owned(directory):
    directory.setup(actor(), options())
    cfg, revision = directory.configuration("1000")
    with pytest.raises(Denied):
        directory._save(
            actor(), cfg.model_copy(update={"recorder_bot_ids": frozenset({"5001"})}), revision
        )
