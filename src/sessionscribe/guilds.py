"""One bot identity, persistent Discord guild configuration and guild-scoped routing.

The transport supplies authenticated interaction permissions and resolved guild
channels/roles. No Discord token, network client or host command is stored here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter

from .control import Controller, Denied
from .files import SafeFiles
from .models import (
    Actor,
    Config,
    Grant,
    InferenceSettings,
    Limits,
    Mode,
    Session,
    SessionState,
    Snowflake,
    StrictModel,
)
from .store import Conflict, Store

MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
ACTIONS = ("start", "resume", "pause", "stop", "status", "process", "result", "delete")


class AdminActor(Actor):
    permissions: int = Field(ge=0)

    def authorize_setup(self):
        if not self.permissions & (MANAGE_GUILD | ADMINISTRATOR):
            raise Denied("Manage Server permission required for guild configuration")


class GuildChannel(StrictModel):
    id: Snowflake
    guild_id: Snowflake
    kind: Literal["text", "voice"]


class GuildRole(StrictModel):
    id: Snowflake
    guild_id: Snowflake


class Setup(StrictModel):
    mode: Mode
    command_channel: GuildChannel
    voice_channel: GuildChannel
    output_channel: GuildChannel
    operator_roles: tuple[GuildRole, ...] = ()


class GuildDirectory:
    def __init__(
        self,
        root: Path,
        inference: InferenceSettings,
        *,
        limits: Limits | None = None,
        recorder_bot_ids: frozenset[str] = frozenset(),
    ):
        self.recorder_bot_ids = TypeAdapter(frozenset[Snowflake]).validate_python(recorder_bot_ids)
        self.files, self.inference, self.limits = SafeFiles(root), inference, limits or Limits()
        path = self.files.root / "guild-config.sqlite3"
        if path.is_symlink():
            raise ValueError("symlink guild registry")
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        os.chmod(path, 0o600)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise ValueError("unsupported guild configuration schema")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS configurations (
            guild_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)""")
        self.db.execute("PRAGMA user_version=2")

    def configuration(self, guild_id: str) -> tuple[Config, int]:
        guild_id = TypeAdapter(Snowflake).validate_python(guild_id, strict=True)
        row = self.db.execute(
            "SELECT body,revision FROM configurations WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if row is None:
            raise KeyError("guild setup required")
        config = Config.model_validate_json(row[0])
        if config.guild_id != guild_id:
            raise ValueError("cross-guild configuration")
        return config, row[1]

    def store(self, guild_id: str) -> Store:
        self.configuration(guild_id)
        return Store(self.files.root / "guilds" / guild_id, guild_id)

    def setup(self, actor: AdminActor, options: Setup) -> Config:
        actor.authorize_setup()
        for channel, kind in (
            (options.command_channel, "text"),
            (options.voice_channel, "voice"),
            (options.output_channel, "text"),
        ):
            if channel.guild_id != actor.guild_id or channel.kind != kind:
                raise Denied("channel does not belong to this guild or has wrong type")
        if any(role.guild_id != actor.guild_id for role in options.operator_roles):
            raise Denied("role does not belong to this guild")
        try:
            previous, revision = self.configuration(actor.guild_id)
        except KeyError:
            previous, revision = None, 0
        grants = {
            action: Grant(
                users=frozenset({actor.user_id}),
                roles=frozenset(role.id for role in options.operator_roles),
            )
            for action in ACTIONS
        }
        config = Config(
            mode=options.mode,
            guild_id=actor.guild_id,
            command_channel_ids=frozenset({options.command_channel.id}),
            voice_channel_ids=(previous.voice_channel_ids if previous else frozenset())
            | {options.voice_channel.id},
            output_channel_id=options.output_channel.id,
            default_protocol_channel_id=(
                previous.default_protocol_channel_id if previous else options.output_channel.id
            ),
            voice_protocol_channels=previous.voice_protocol_channels if previous else {},
            recorder_bot_ids=previous.recorder_bot_ids if previous else self.recorder_bot_ids,
            grants=grants,
            inference=self.inference,
            limits=self.limits,
            recording_enabled=previous.recording_enabled if previous else False,
            cloud_enabled=previous.cloud_enabled if previous else False,
            daily_cloud_calls=previous.daily_cloud_calls if previous else 4,
            daily_cloud_tokens=previous.daily_cloud_tokens if previous else 100_000,
        )
        self._save(actor, config, revision)
        return config

    def _save(self, actor: AdminActor, config: Config, expected_revision: int):
        with self.files.lock(f"guild-locks/{actor.guild_id}.lock") as acquired:
            if not acquired:
                raise Conflict("guild configuration or action in progress")
            self._save_locked(actor, config, expected_revision)

    def _save_locked(self, actor: AdminActor, config: Config, expected_revision: int):
        actor.authorize_setup()
        if config.guild_id != actor.guild_id or config.recorder_bot_ids - self.recorder_bot_ids:
            raise Denied("cross-guild configuration update")
        if expected_revision:
            store = self.store(actor.guild_id)
            try:
                active = any(
                    Session.model_validate_json(row[0]).state
                    in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED)
                    for row in store.db.execute("SELECT body FROM sessions")
                )
                queued = store.db.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('pending','running')"
                ).fetchone()
                pending_output = store.db.execute(
                    "SELECT 1 FROM protocol_outbox WHERE state IN ('pending','sending')"
                ).fetchone()
                if active or queued or pending_output:
                    raise Conflict(
                        "finish recording and queued work before changing guild settings"
                    )
            finally:
                store.close()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if expected_revision == 0:
                try:
                    self.db.execute(
                        "INSERT INTO configurations VALUES (?,1,?)",
                        (actor.guild_id, config.model_dump_json()),
                    )
                except sqlite3.IntegrityError:
                    raise Conflict("guild configuration changed") from None
            else:
                updated = self.db.execute(
                    """UPDATE configurations SET revision=revision+1,body=?
                    WHERE guild_id=? AND revision=?""",
                    (config.model_dump_json(), actor.guild_id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise Conflict("guild configuration changed")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def show(self, actor: AdminActor) -> Config:
        actor.authorize_setup()
        return self.configuration(actor.guild_id)[0]

    def set_mode(self, actor: AdminActor, mode: Mode) -> Config:
        actor.authorize_setup()
        config, revision = self.configuration(actor.guild_id)
        mode = TypeAdapter(Mode).validate_python(mode, strict=True)
        updated = Config.model_validate(config.model_dump() | {"mode": mode})
        self._save(actor, updated, revision)
        return updated

    def set_roles(self, actor: AdminActor, action: str, roles: tuple[GuildRole, ...]) -> Config:
        actor.authorize_setup()
        if action not in ACTIONS or any(role.guild_id != actor.guild_id for role in roles):
            raise Denied("action or role denied")
        config, revision = self.configuration(actor.guild_id)
        grants = config.grants | {
            action: Grant(
                users=config.grants[action].users, roles=frozenset(role.id for role in roles)
            )
        }
        updated = Config.model_validate(config.model_dump() | {"grants": grants})
        self._save(actor, updated, revision)
        return updated

    def set_default_protocol(self, actor: AdminActor, channel: GuildChannel | None) -> Config:
        actor.authorize_setup()
        if channel and (channel.guild_id != actor.guild_id or channel.kind != "text"):
            raise Denied("protocol channel must be a text channel in this guild")
        config, revision = self.configuration(actor.guild_id)
        updated = Config.model_validate(
            config.model_dump() | {"default_protocol_channel_id": channel.id if channel else None}
        )
        self._save(actor, updated, revision)
        return updated

    def set_voice_protocol(
        self,
        actor: AdminActor,
        voice: GuildChannel,
        channel: GuildChannel | None,
        *,
        inherit: bool = False,
    ) -> Config:
        actor.authorize_setup()
        if voice.guild_id != actor.guild_id or voice.kind != "voice":
            raise Denied("voice channel must belong to this guild")
        if channel and (channel.guild_id != actor.guild_id or channel.kind != "text"):
            raise Denied("protocol channel must be a text channel in this guild")
        if inherit and channel is not None:
            raise ValueError("choose explicit protocol channel or inheritance")
        config, revision = self.configuration(actor.guild_id)
        routes = dict(config.voice_protocol_channels)
        if inherit:
            routes.pop(voice.id, None)
        else:
            routes[voice.id] = channel.id if channel else None
        updated = Config.model_validate(
            config.model_dump()
            | {
                "voice_channel_ids": config.voice_channel_ids | {voice.id},
                "voice_protocol_channels": routes,
            }
        )
        self._save(actor, updated, revision)
        return updated

    @contextlib.contextmanager
    def controller(self, actor: Actor, status, close_audio):
        with self.files.lock(f"guild-locks/{actor.guild_id}.lock") as acquired:
            if not acquired:
                raise Conflict("guild configuration or action in progress")
            config, _ = self.configuration(actor.guild_id)
            store = self.store(actor.guild_id)
            try:
                yield Controller(config, store, status, close_audio)
            finally:
                store.close()

    def close(self):
        self.db.close()


class GuildCommands:
    def __init__(self, directory: GuildDirectory):
        self.directory = directory

    def setup(self, actor: AdminActor, options: Setup):
        return self.directory.setup(actor, options)

    def show(self, actor: AdminActor):
        return self.directory.show(actor)

    def mode(self, actor: AdminActor, mode: Mode):
        return self.directory.set_mode(actor, mode)

    def roles(self, actor: AdminActor, action: str, roles: tuple[GuildRole, ...]):
        return self.directory.set_roles(actor, action, roles)

    def default_protocol(self, actor: AdminActor, channel: GuildChannel | None):
        return self.directory.set_default_protocol(actor, channel)

    def protocol(
        self,
        actor: AdminActor,
        voice: GuildChannel,
        channel: GuildChannel | None,
        *,
        inherit: bool = False,
    ):
        return self.directory.set_voice_protocol(actor, voice, channel, inherit=inherit)


def definitions() -> list[dict]:
    choices = [{"name": "Meeting", "value": "meeting"}, {"name": "DnD", "value": "dnd"}]
    commands = [
        {
            "name": "setup",
            "description": "SessionScribe für diesen Server einrichten",
            "type": 1,
            "dm_permission": False,
            "default_member_permissions": str(MANAGE_GUILD),
            "options": [
                {
                    "name": "mode",
                    "description": "Betriebsmodus",
                    "type": 3,
                    "required": True,
                    "choices": choices,
                },
                {
                    "name": "command_channel",
                    "description": "Befehlskanal",
                    "type": 7,
                    "required": True,
                    "channel_types": [0],
                },
                {
                    "name": "voice_channel",
                    "description": "Sprachkanal",
                    "type": 7,
                    "required": True,
                    "channel_types": [2],
                },
                {
                    "name": "output_channel",
                    "description": "Ergebniskanal",
                    "type": 7,
                    "required": True,
                    "channel_types": [0],
                },
            ],
        },
        {
            "name": "config",
            "description": "Serverkonfiguration anzeigen oder ändern",
            "type": 1,
            "dm_permission": False,
            "default_member_permissions": str(MANAGE_GUILD),
            "options": [
                {"name": "show", "description": "Konfiguration anzeigen", "type": 1},
                {
                    "name": "mode",
                    "description": "Meeting oder DnD wählen",
                    "type": 1,
                    "options": [
                        {
                            "name": "mode",
                            "description": "Betriebsmodus",
                            "type": 3,
                            "required": True,
                            "choices": choices,
                        }
                    ],
                },
            ],
        },
    ]

    commands[1]["options"].append(
        {
            "name": "acl",
            "description": "Rolle für eine Aktion festlegen",
            "type": 1,
            "options": [
                {
                    "name": "action",
                    "description": "Aktion",
                    "type": 3,
                    "required": True,
                    "choices": [{"name": action, "value": action} for action in ACTIONS],
                },
                {"name": "role", "description": "Berechtigte Rolle", "type": 8, "required": True},
            ],
        }
    )
    commands[1]["options"].extend(
        [
            {
                "name": "default_protocol",
                "description": "Default-Protokollkanal setzen oder entfernen",
                "type": 1,
                "options": [
                    {
                        "name": "channel",
                        "description": "Protokollkanal; leer zum Abschalten",
                        "type": 7,
                        "channel_types": [0],
                        "required": False,
                    }
                ],
            },
            {
                "name": "protocol",
                "description": "Protokollziel für einen Sprachkanal",
                "type": 1,
                "options": [
                    {
                        "name": "voice_channel",
                        "description": "Sprachkanal",
                        "type": 7,
                        "required": True,
                        "channel_types": [2],
                    },
                    {
                        "name": "channel",
                        "description": "Protokollziel; leer zum Abschalten",
                        "type": 7,
                        "channel_types": [0],
                        "required": False,
                    },
                    {
                        "name": "inherit",
                        "description": "Default des Servers verwenden",
                        "type": 5,
                        "required": False,
                    },
                ],
            },
        ]
    )
    return commands
