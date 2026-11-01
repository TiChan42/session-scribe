import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import discord.ext.commands
import pytest
from pydantic import SecretStr

from sessionscribe import files, voice_test
from sessionscribe.control import Denied
from sessionscribe.models import InferenceSettings


def policy():
    return voice_test.TestPolicy(
        purpose="authorized-single-person-dave-test",
        guild_id="1000",
        voice_channel_id="1001",
        protocol_channel_id="1002",
        participant_id="1003",
        expires_at=0,
        cloud_consent=True,
        inference=InferenceSettings(model="synthetic", context_tokens=8192, output_tokens=2048),
    )


def context(user=1003, guild_id=1000, channel_id=1002, permissions=32):
    guild = SimpleNamespace(id=guild_id)
    author = Mock(spec=discord.Member)
    author.id, author.guild, author.roles = user, guild, []
    author.guild_permissions = discord.Permissions(permissions)
    return SimpleNamespace(
        guild=guild,
        author=author,
        channel=SimpleNamespace(id=channel_id, guild=guild),
        defer=AsyncMock(),
        respond=AsyncMock(),
    )


def test_manual_test_requires_authenticated_owner_guild_channel_and_permission():
    assert voice_test.manual_actor(policy(), context()).user_id == "1003"
    for change in ({"user": 1004}, {"guild_id": 2000}, {"channel_id": 1001}, {"permissions": 0}):
        with pytest.raises(Denied):
            voice_test.manual_actor(policy(), context(**change))


def test_listener_registers_without_recording_then_only_explicit_callback_runs(
    monkeypatch, tmp_path
):
    async def scenario():
        client = discord.Bot(intents=discord.Intents.none(), auto_sync_commands=False)
        actual_files = files.SafeFiles
        monkeypatch.setattr(files, "SafeFiles", lambda _root: actual_files(tmp_path))
        monkeypatch.setattr(discord, "Bot", lambda **_kwargs: client)
        monkeypatch.setattr(
            voice_test, "discord_token", lambda _path: SecretStr("synthetic-only-token")
        )
        backend = AsyncMock(return_value={"dave_single_speaker_receive": True})
        monkeypatch.setattr(voice_test, "test", backend)
        client.http.upsert_guild_command = AsyncMock(return_value={"id": "7000"})
        client.http.get_guild_commands = AsyncMock(
            return_value=[{"id": "7000", "name": "testaufnahme"}]
        )
        client.close = AsyncMock()

        async def start(_token):
            await client.on_ready()
            backend.assert_not_awaited()
            command = next(
                c for c in client.pending_application_commands if c.name == "testaufnahme"
            )
            assert command.guild_ids == [1000]
            await command.callback(context(user=1004))
            backend.assert_not_awaited()
            await command.callback(context())
            backend.assert_awaited_once()
            assert backend.call_args.args[0].expires_at > 0

        client.start = start
        await voice_test.listen(policy())
        client.http.upsert_guild_command.assert_awaited_once()
        assert client.http.upsert_guild_command.call_args.args[1] == 1000

    asyncio.run(scenario())
