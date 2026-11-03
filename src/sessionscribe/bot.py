"""Discord control transport. Live voice reception remains deliberately unavailable.

SQLite and synchronous policy run on one dedicated thread. Discord I/O remains on
the event loop. Neither errors nor health records contain credentials or content.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from .commands import CommandRouter, canonical_command
from .commands import definitions as session_definitions
from .control import Controller, Denied
from .credentials import discord_token
from .files import SafeFiles
from .guilds import (
    AdminActor,
    GuildChannel,
    GuildDirectory,
    GuildRole,
    Setup,
)
from .guilds import definitions as guild_definitions
from .models import Identifier, InferenceSettings, Limits, SessionState, Snowflake, StrictModel
from .render import ProtocolPublisher
from .store import Conflict
from .voice import ReceiveUnavailable


class BotPolicy(StrictModel):
    data_root: Path
    token_file: Path
    revision: str = Field(pattern=r"^(development|[a-f0-9]{40,64})$")
    inference: InferenceSettings
    limits: Limits = Limits()
    allowed_guild_ids: frozenset[Snowflake] = frozenset()
    max_guilds: int = Field(default=32, ge=1, le=1000)
    # These fields cannot unlock unreviewed reception or an absent worker launcher.
    voice_reception: Literal["disabled"] = "disabled"
    cloud_processing: Literal["disabled"] = "disabled"

    @model_validator(mode="after")
    def absolute_paths(self):
        if not self.data_root.is_absolute() or not self.token_file.is_absolute():
            raise ValueError("runtime paths must be absolute")
        return self


class ControlService:
    """Called only from the single policy executor, never the Discord event loop."""

    def __init__(self, policy: BotPolicy, status, close_audio):
        self.policy, self.status, self.close_audio = policy, status, close_audio
        self.directory = GuildDirectory(policy.data_root, policy.inference, limits=policy.limits)
        self.publish_cursor = 0

    def allow(self, actor: AdminActor):
        if self.policy.allowed_guild_ids and actor.guild_id not in self.policy.allowed_guild_ids:
            raise Denied("guild not enabled by the operator")

    def configure(self, name: str, actor: AdminActor, options: dict):
        self.allow(actor)
        actor.authorize_setup()
        directory = self.directory
        if name == "setup":
            exists = directory.db.execute(
                "SELECT 1 FROM configurations WHERE guild_id=?", (actor.guild_id,)
            ).fetchone()
            count = directory.db.execute("SELECT count(*) FROM configurations").fetchone()[0]
            if not exists and count >= self.policy.max_guilds:
                raise Denied("operator guild capacity reached")
            return directory.setup(actor, Setup.model_validate(options))
        if name == "show":
            return directory.show(actor)
        if name == "mode":
            return directory.set_mode(actor, options["mode"])
        if name == "acl":
            return directory.set_roles(actor, options["action"], (options["role"],))
        if name == "default_protocol":
            return directory.set_default_protocol(actor, options.get("channel"))
        if name == "protocol":
            return directory.set_voice_protocol(
                actor,
                options["voice_channel"],
                options.get("channel"),
                inherit=options.get("inherit", False),
            )
        raise ValueError("unknown configuration command")

    def session(
        self,
        name: str,
        actor: AdminActor,
        options: dict,
        *,
        current_participants: frozenset[str] | None = None,
    ):
        self.allow(actor)
        behavior = canonical_command(name)
        if behavior == "alias":
            return self.alias(
                actor,
                alias=options.get("alias"),
                recording_consent=options.get("recording_consent"),
                current_participants=current_participants,
            )
        if behavior == "alias-delete":
            return self.delete_alias(actor)
        if "session_id" in options:
            TypeAdapter(Identifier).validate_python(options["session_id"], strict=True)
        with self.directory.controller(
            actor,
            lambda channel, message: self.status(actor.guild_id, channel, message),
            lambda sid: self.close_audio(actor.guild_id, sid),
        ) as controller:
            if behavior in ("start", "resume"):
                controller.authorize(actor, behavior)
                if behavior == "resume":
                    if current_participants is None:
                        raise Denied("fresh voice membership is required to resume")
                    target = controller.owned(actor, options["session_id"], "resume")
                    if target.voice_channel_id not in controller.config.voice_channel_ids:
                        raise Denied("session voice channel is outside this guild")
                    controller.membership(
                        target.id,
                        current_participants,
                        speaker_aliases=self.directory.voice_aliases(
                            actor.guild_id, target.voice_channel_id
                        ),
                    )
                raise ReceiveUnavailable("DAVE reception awaits review and authorized tests")
            if behavior == "process":
                controller.owned(actor, options["session_id"], "process")
                raise ReceiveUnavailable("native processing workers are not enabled")
            return CommandRouter(controller).dispatch(behavior, actor, **options)

    def alias(
        self,
        actor: AdminActor,
        *,
        alias: str | None,
        recording_consent: str | None,
        current_participants: frozenset[str] | None,
    ) -> dict:
        self.allow(actor)
        config, _revision = self.directory.configuration(actor.guild_id)
        if (
            actor.voice_channel_id is None
            or actor.channel_id not in config.command_channel_ids
            or actor.voice_channel_id not in config.voice_channel_ids
        ):
            raise Denied("a configured voice and command channel are required")
        if recording_consent is not None and recording_consent not in ("accept", "decline"):
            raise ValueError("invalid recording consent choice")

        store = self.directory.store(actor.guild_id)
        try:
            controller = Controller(
                config,
                store,
                lambda channel, message: self.status(actor.guild_id, channel, message),
                lambda sid: self.close_audio(actor.guild_id, sid),
            )
            active = None
            for row in store.db.execute("SELECT id FROM sessions").fetchall():
                try:
                    session = store.session(row[0])
                except KeyError:
                    continue
                if session.voice_channel_id == actor.voice_channel_id and session.state in (
                    SessionState.WAITING,
                    SessionState.RECORDING,
                    SessionState.PAUSED,
                ):
                    active = session
                    break
            if active is not None:
                if active.status_channel_id != actor.channel_id:
                    raise Denied("use the command channel that started this session")
                if current_participants is None or actor.user_id not in current_participants:
                    raise Denied("fresh voice membership is required for recording consent")
                if recording_consent is None:
                    raise ValueError("recording consent choice required for an active session")

            if alias is not None:
                bound_alias = self.directory.set_voice_alias(
                    actor.guild_id, actor.voice_channel_id, actor.user_id, alias
                )
            else:
                bound_alias = self.directory.voice_aliases(
                    actor.guild_id, actor.voice_channel_id
                ).get(actor.user_id)
                if bound_alias is None:
                    raise ValueError("enter an alias before confirming consent")

            if active is None:
                # Aliases persist, but consent is never carried into an unknown future session.
                return {"alias_saved": True, "consent_applied": False}
            controller.membership(
                active.id,
                current_participants,
                speaker_aliases=self.directory.voice_aliases(
                    actor.guild_id, actor.voice_channel_id
                ),
            )
            updated = controller.alias_consent(
                actor,
                active.id,
                bound_alias,
                accepted=recording_consent == "accept",
            )
            return {
                "alias_saved": True,
                "consent_applied": True,
                "recording_consent": recording_consent,
                "state": updated.state.value,
            }
        finally:
            store.close()

    def session_voice_channel(self, actor: AdminActor, sid: str) -> str:
        """Return a session's configured voice channel for a fresh Discord cache read."""
        TypeAdapter(Identifier).validate_python(sid, strict=True)
        self.allow(actor)
        with self.directory.controller(
            actor,
            lambda channel, message: self.status(actor.guild_id, channel, message),
            lambda session_id: self.close_audio(actor.guild_id, session_id),
        ) as controller:
            session = controller.owned(actor, sid, "resume")
            if session.state not in (
                SessionState.WAITING,
                SessionState.RECORDING,
                SessionState.PAUSED,
            ):
                raise Conflict("session is already finalized")
            return session.voice_channel_id

    def delete_alias(self, actor: AdminActor) -> bool:
        self.allow(actor)
        voice_channel_id = actor.voice_channel_id
        if voice_channel_id is None:
            raise Denied("join the configured voice channel first")
        config, _revision = self.directory.configuration(actor.guild_id)
        if (
            actor.channel_id not in config.command_channel_ids
            or voice_channel_id not in config.voice_channel_ids
        ):
            raise Denied("a configured voice and command channel are required")
        self.directory.delete_voice_alias(actor.guild_id, voice_channel_id, actor.user_id)
        store = self.directory.store(actor.guild_id)
        try:
            controller = Controller(
                config,
                store,
                lambda channel, message: self.status(actor.guild_id, channel, message),
                lambda sid: self.close_audio(actor.guild_id, sid),
            )
            for row in store.db.execute("SELECT id FROM sessions").fetchall():
                try:
                    session = store.session(row[0])
                except KeyError:
                    continue
                if (
                    session.voice_channel_id == voice_channel_id
                    and actor.user_id in session.participants
                    and session.state
                    in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED)
                ):
                    controller.remove_session_alias(actor, session.id)
            return True
        finally:
            store.close()

    def voice_membership(
        self, guild_id: str, voice_channel_id: str, participants: frozenset[str]
    ) -> bool:
        if self.policy.allowed_guild_ids and guild_id not in self.policy.allowed_guild_ids:
            return False
        try:
            config, _revision = self.directory.configuration(guild_id)
        except KeyError:
            return False
        if voice_channel_id not in config.voice_channel_ids:
            return False
        store = self.directory.store(guild_id)
        try:
            controller = Controller(
                config,
                store,
                lambda channel, message: self.status(guild_id, channel, message),
                lambda sid: self.close_audio(guild_id, sid),
            )
            aliases = self.directory.voice_aliases(guild_id, voice_channel_id)
            updated = False
            for row in store.db.execute("SELECT id FROM sessions").fetchall():
                try:
                    session = store.session(row[0])
                except KeyError:
                    continue
                if session.voice_channel_id == voice_channel_id and session.state in (
                    SessionState.WAITING,
                    SessionState.RECORDING,
                    SessionState.PAUSED,
                ):
                    controller.membership(session.id, participants, speaker_aliases=aliases)
                    updated = True
            return updated
        finally:
            store.close()

    def remind_consent(self, now: int | None = None) -> int:
        guilds = [
            row[0]
            for row in self.directory.db.execute(
                "SELECT guild_id FROM configurations ORDER BY guild_id"
            )
            if not self.policy.allowed_guild_ids or row[0] in self.policy.allowed_guild_ids
        ]
        reminders = 0
        for guild_id in guilds:
            config, _revision = self.directory.configuration(guild_id)
            store = self.directory.store(guild_id)
            try:
                controller = Controller(
                    config,
                    store,
                    lambda channel, message, guild_id=guild_id: self.status(
                        guild_id, channel, message
                    ),
                    lambda sid, guild_id=guild_id: self.close_audio(guild_id, sid),
                )
                for row in store.db.execute("SELECT id FROM sessions").fetchall():
                    try:
                        session = store.session(row[0])
                    except KeyError:
                        continue
                    if controller.remind_consent(session.id, now):
                        reminders += 1
            finally:
                store.close()
        return reminders

    def pause_for_voice_state_loss(self) -> int:
        guilds = [
            row[0]
            for row in self.directory.db.execute(
                "SELECT guild_id FROM configurations ORDER BY guild_id"
            )
            if not self.policy.allowed_guild_ids or row[0] in self.policy.allowed_guild_ids
        ]
        paused = 0
        for guild_id in guilds:
            config, _revision = self.directory.configuration(guild_id)
            store = self.directory.store(guild_id)
            try:
                controller = Controller(
                    config,
                    store,
                    lambda channel, message, guild_id=guild_id: self.status(
                        guild_id, channel, message
                    ),
                    lambda sid, guild_id=guild_id: self.close_audio(guild_id, sid),
                )
                for row in store.db.execute("SELECT id FROM sessions").fetchall():
                    try:
                        if controller.pause_for_voice_state_loss(row[0]):
                            paused += 1
                    except KeyError:
                        continue
            finally:
                store.close()
        return paused

    def close(self):
        self.directory.close()

    def publish_once(self, delivery_factory) -> bool:
        guilds = [
            row[0]
            for row in self.directory.db.execute(
                "SELECT guild_id FROM configurations ORDER BY guild_id"
            )
            if not self.policy.allowed_guild_ids or row[0] in self.policy.allowed_guild_ids
        ]
        if not guilds:
            return False
        for _ in guilds:
            guild = guilds[self.publish_cursor % len(guilds)]
            self.publish_cursor = (self.publish_cursor + 1) % len(guilds)
            config, _revision = self.directory.configuration(guild)
            store = self.directory.store(guild)
            try:
                if ProtocolPublisher(store, config, delivery_factory(guild)).once():
                    return True
            finally:
                store.close()
        return False


def authenticated_actor(ctx) -> AdminActor:
    """Require a genuine guild interaction/member; never accept identity options."""
    import discord

    if (
        ctx.guild is None
        or not isinstance(ctx.author, discord.Member)
        or ctx.author.guild.id != ctx.guild.id
        or ctx.channel is None
        or getattr(ctx.channel, "guild", None) is None
        or ctx.channel.guild.id != ctx.guild.id
    ):
        raise Denied("guild interaction required")
    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None)
    voice_channel_id = None
    if voice_channel is not None:
        if (
            not isinstance(voice_channel, discord.VoiceChannel)
            or voice_channel.guild.id != ctx.guild.id
        ):
            raise Denied("caller voice state is not a channel in this guild")
        voice_channel_id = str(voice_channel.id)
    return AdminActor(
        guild_id=str(ctx.guild.id),
        channel_id=str(ctx.channel.id),
        user_id=str(ctx.author.id),
        role_ids=frozenset(str(role.id) for role in ctx.author.roles),
        permissions=ctx.author.guild_permissions.value,
        voice_channel_id=voice_channel_id,
    )


def resolved_channel(
    guild, value, kind: Literal["text", "voice"], *, attachments: bool = True
) -> GuildChannel:
    import discord

    # Use the gateway's actual guild cache, not a partial interaction fallback that
    # can lack overwrites. The policy layer independently checks guild and type.
    channel = guild.get_channel(value.id)
    expected = discord.TextChannel if kind == "text" else discord.VoiceChannel
    if not isinstance(channel, expected) or channel.guild.id != guild.id:
        raise Denied("channel is not a cached channel of the required guild and type")
    permissions = channel.permissions_for(guild.me)
    if not permissions.view_channel or (kind == "text" and not permissions.send_messages):
        raise Denied("bot needs channel view and text send permissions")
    if kind == "text" and attachments and not permissions.attach_files:
        raise Denied("bot needs Attach Files for bounded protocol attachments")
    return GuildChannel(id=str(channel.id), guild_id=str(guild.id), kind=kind)


class DiscordDelivery:
    """Synchronous publisher bridge, used exclusively outside the event-loop thread."""

    def __init__(self, client, loop, guild_id: str):
        self.client, self.loop = client, loop
        self.guild_id = TypeAdapter(Snowflake).validate_python(guild_id, strict=True)

    async def _send(self, channel_id, content=None, filename=None, data=None):
        import discord

        guild = self.client.get_guild(int(self.guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != int(self.guild_id):
            raise Denied("protocol destination is not in the configured guild")
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.send_messages:
            raise Denied("protocol destination permissions denied")
        kwargs = {
            "allowed_mentions": discord.AllowedMentions.none(),
            "suppress_embeds": True,
        }
        if data is not None:
            if (
                not permissions.attach_files
                or not re.fullmatch(r"protocol-[a-f0-9]{32}\.md", filename or "")
                or len(data) > 2_000_000
            ):
                raise Denied("protocol attachment denied")
            kwargs["file"] = discord.File(io.BytesIO(data), filename=filename)
        elif not isinstance(content, str) or len(content) > 1900:
            raise ValueError("invalid bounded Discord message")
        message = await channel.send(content=content, **kwargs)
        return str(message.id)

    async def _send_files(self, channel_id, files):
        import discord

        guild = self.client.get_guild(int(self.guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != int(self.guild_id):
            raise Denied("protocol destination is not in the configured guild")
        if (
            not channel.permissions_for(guild.me).view_channel
            or not channel.permissions_for(guild.me).send_messages
            or not channel.permissions_for(guild.me).attach_files
        ):
            raise Denied("protocol destination permissions denied")
        expected = {"protokoll-begruendet.md", "protokoll-interpretiert.md"}
        if set(files) != expected or sum(map(len, files.values())) > 2_000_000:
            raise Denied("protocol attachments denied")
        uploads = [discord.File(io.BytesIO(files[name]), filename=name) for name in sorted(files)]
        message = await channel.send(
            content="SessionScribe: zwei geprüfte Protokollfassungen.",
            files=uploads,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
        return str(message.id)

    def _submit(self, coroutine):
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is self.loop:
                coroutine.close()
                raise Denied("synchronous delivery cannot run on the Discord event loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            # An unacknowledged write remains uncertain in the durable outbox.
            raise OSError("Discord delivery was not acknowledged") from None

    def send(self, channel_id: str, content: str, *, allowed_mentions: dict) -> str:
        return self._submit(self._send(channel_id, content))

    def send_file(
        self, channel_id: str, filename: str, data: bytes, *, allowed_mentions: dict
    ) -> str:
        return self._submit(self._send(channel_id, filename=filename, data=data))

    def send_files(
        self, channel_id: str, files: dict[str, bytes], *, allowed_mentions: dict
    ) -> str:
        return self._submit(self._send_files(channel_id, files))


def register_commands(client, invoke):
    """Build Pycord commands from the same reviewed schemas used by policy tests."""
    import discord

    def register(definition, parent=None):
        if definition["name"] == "config" and parent is None:
            group = discord.SlashCommandGroup(
                "config",
                definition["description"],
                contexts={discord.InteractionContextType.guild},
                default_member_permissions=discord.Permissions(manage_guild=True),
                integration_types={discord.IntegrationType.guild_install},
            )
            for child in definition["options"]:
                register(child, group)
            client.add_application_command(group)
            return

        async def callback(ctx, **options):
            await invoke(ctx, definition["name"], options, config=parent is not None)

        parameters = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        options = []
        for option in definition.get("options", []):
            attributes = {k: v for k, v in option.items() if k not in ("name", "type")}
            if "choices" in attributes:
                attributes["choices"] = [discord.OptionChoice(**x) for x in attributes["choices"]]
            if "channel_types" in attributes:
                attributes["channel_types"] = [
                    discord.ChannelType(x) for x in attributes["channel_types"]
                ]
            options.append(
                discord.Option(
                    discord.SlashCommandOptionType(option["type"]),
                    name=option["name"],
                    **attributes,
                )
            )
            parameters.append(
                inspect.Parameter(
                    option["name"],
                    inspect.Parameter.KEYWORD_ONLY,
                    default=(inspect.Parameter.empty if option.get("required", False) else None),
                )
            )
        # Pycord matches explicitly provided Options to the callback signature;
        # this constructs metadata, never executable source or model output.
        callback.__signature__ = inspect.Signature(parameters)
        kwargs = {
            "name": definition["name"],
            "description": definition["description"],
            "options": options,
            "contexts": {discord.InteractionContextType.guild},
            "integration_types": {discord.IntegrationType.guild_install},
        }
        if definition["name"] == "setup":
            kwargs["default_member_permissions"] = discord.Permissions(manage_guild=True)
        command = discord.SlashCommand(callback, parent=parent, **kwargs)
        if parent:
            parent.add_command(command)
        else:
            client.add_application_command(command)

    for definition in guild_definitions() + session_definitions():
        register(definition)


class ControlBot:
    def __init__(self, policy: BotPolicy):
        import discord

        self.policy = policy
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Bot(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            auto_sync_commands=False,
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="control-policy")
        self.service = None
        self.files = SafeFiles(policy.data_root)
        self.loop = None
        self.ready = False
        self.commands_ready = False
        self.command_slots = asyncio.Semaphore(16)
        register_commands(self.client, self.invoke)
        self.client.event(self.on_ready)
        self.client.event(self.on_disconnect)
        self.client.event(self.on_voice_state_update)
        self.client.event(self.on_application_command_error)
        self.client.event(self.on_error)

    async def policy_call(self, fn, *args, **kwargs):
        return await self.loop.run_in_executor(
            self.executor, functools.partial(fn, *args, **kwargs)
        )

    async def send_status(self, guild_id: str, channel_id: str, message: str) -> str | None:
        import discord

        try:
            guild_id = TypeAdapter(Snowflake).validate_python(guild_id, strict=True)
            channel_id = TypeAdapter(Snowflake).validate_python(channel_id, strict=True)
        except Exception:
            return None
        if not isinstance(message, str) or not 1 <= len(message) <= 1800:
            return None
        guild = self.client.get_guild(int(guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        member = guild.me if guild else None
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != int(guild_id)
            or member is None
            or not channel.permissions_for(member).view_channel
            or not channel.permissions_for(member).send_messages
        ):
            return None
        sent = await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
        return str(sent.id)

    async def on_ready(self):
        if not self.commands_ready:
            try:
                async with asyncio.timeout(60):
                    await self.client.sync_commands()
                    commands = await self.client.http.get_global_commands(
                        self.client.application_id
                    )
                    expected = {
                        item["name"] for item in guild_definitions() + session_definitions()
                    }
                    self.commands_ready = expected <= {item["name"] for item in commands}
            except Exception:
                logging.getLogger(__name__).error("Command registration failed; details suppressed")
        self.ready = self.commands_ready
        self.health()

    async def on_disconnect(self):
        self.ready = False
        if self.service is not None:
            with contextlib.suppress(Exception):
                await self.policy_call(self.service.pause_for_voice_state_loss)
        self.health()

    async def on_voice_state_update(self, member, before, after):
        if self.service is None or member.bot:
            return
        import discord

        changed = {getattr(before, "channel", None), getattr(after, "channel", None)}
        changed.discard(None)
        for channel in changed:
            if not isinstance(channel, discord.VoiceChannel):
                continue
            participants = {str(person.id) for person in channel.members if not person.bot}
            if getattr(getattr(before, "channel", None), "id", None) == channel.id:
                participants.discard(str(member.id))
            if getattr(getattr(after, "channel", None), "id", None) == channel.id:
                participants.add(str(member.id))
            with contextlib.suppress(Exception):
                await self.policy_call(
                    self.service.voice_membership,
                    str(member.guild.id),
                    str(channel.id),
                    frozenset(participants),
                )

    async def on_error(self, _event, *_args, **_kwargs):
        logging.getLogger(__name__).error("Discord event failed; details suppressed")

    async def on_application_command_error(self, ctx, _error):
        with contextlib.suppress(Exception):
            await ctx.respond("Befehl fehlgeschlagen. Bitte erneut prüfen.", ephemeral=True)

    def health(self):
        self.files.atomic(
            "control-health.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "revision": self.policy.revision,
                    "checked_at": time.time(),
                    "control_ready": self.ready,
                    "voice_ready": False,
                    "cloud_ready": False,
                }
            ).encode(),
        )

    async def heartbeat(self):
        while True:
            self.health()
            if self.ready and self.service is not None:
                try:
                    await self.policy_call(self.service.remind_consent)
                    await self.policy_call(
                        self.service.publish_once,
                        lambda guild: DiscordDelivery(self.client, self.loop, guild),
                    )
                except Exception:
                    logging.getLogger(__name__).error("Protocol poll failed; details suppressed")
            await asyncio.sleep(30)

    def prepare_options(self, ctx, name, options):
        options = dict(options)
        for key in ("command_channel", "voice_channel", "output_channel", "channel"):
            if options.get(key) is not None:
                kind = "voice" if key == "voice_channel" else "text"
                options[key] = resolved_channel(
                    ctx.guild, options[key], kind, attachments=key != "command_channel"
                )
        if name == "acl":
            role = ctx.guild.get_role(options["role"].id)
            if role is None or role.guild.id != ctx.guild.id:
                raise Denied("role does not belong to this guild")
            options["role"] = GuildRole(id=str(role.id), guild_id=str(ctx.guild.id))
        return options

    async def invoke(self, ctx, name, options, *, config):
        if self.command_slots.locked():
            await ctx.respond("Zu viele Befehle; bitte später erneut versuchen.", ephemeral=True)
            return
        async with self.command_slots:
            await self._invoke(ctx, name, options, config=config)

    async def _invoke(self, ctx, name, options, *, config):
        import discord

        await ctx.defer(ephemeral=True)
        try:
            actor = authenticated_actor(ctx)
            if config or name == "setup":
                actor.authorize_setup()
                prepared = self.prepare_options(ctx, name, options)
                result = await self.policy_call(self.service.configure, name, actor, prepared)
            else:
                current_participants = None
                # Session start does not inspect participants or join while gated.
                if "voice_channel" in options:
                    selected = resolved_channel(ctx.guild, options["voice_channel"], "voice")
                    channel = ctx.guild.get_channel(int(selected.id))
                    if not isinstance(channel, discord.VoiceChannel):
                        raise Denied("voice channel membership is unavailable")
                    participants = frozenset(
                        str(member.id) for member in channel.members if not member.bot
                    )
                    options = options | {
                        "voice_channel_id": channel.id,
                        "participants": participants,
                    }
                    options.pop("voice_channel")
                if name == "alias":
                    channel_id = actor.voice_channel_id
                    if channel_id is None:
                        raise Denied("join the configured voice channel first")
                elif name == "resume":
                    channel_id = await self.policy_call(
                        self.service.session_voice_channel,
                        actor,
                        options["session_id"],
                    )
                else:
                    channel_id = None
                if channel_id is not None:
                    channel = ctx.guild.get_channel(int(channel_id))
                    if (
                        not isinstance(channel, discord.VoiceChannel)
                        or channel.guild.id != ctx.guild.id
                    ):
                        raise Denied("voice channel membership is unavailable")
                    current_participants = frozenset(
                        str(member.id) for member in channel.members if not member.bot
                    )
                    if name == "alias":
                        if actor.voice_channel_id != channel_id:
                            raise Denied("join the session voice channel first")
                        current_participants |= {actor.user_id}
                result = await self.policy_call(
                    self.service.session,
                    name,
                    actor,
                    options,
                    current_participants=current_participants,
                )
            if isinstance(result, bytes):
                if len(result) > 2_000_000:
                    raise ValueError("result attachment exceeds bound")
                await ctx.respond(
                    file=discord.File(io.BytesIO(result), filename="result.md"),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if (
                isinstance(result, dict)
                and set(result) == {"protokoll-begruendet.md", "protokoll-interpretiert.md"}
                and all(isinstance(value, bytes) for value in result.values())
            ):
                if sum(map(len, result.values())) > 2_000_000:
                    raise ValueError("result attachments exceed bound")
                await ctx.respond(
                    files=[
                        discord.File(io.BytesIO(data), filename=filename)
                        for filename, data in sorted(result.items())
                    ],
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if isinstance(result, StrictModel):
                result = result.model_dump(mode="json")
            if name == "alias" and isinstance(result, dict):
                if result.get("consent_applied"):
                    text = (
                        "Alias gespeichert. Deine Aufzeichnungsentscheidung gilt nur für "
                        "diese Sitzung. Nach Zustimmung aller Personen muss die Leitung "
                        "mit /resume fortsetzen."
                        if result.get("recording_consent") == "accept"
                        else "Alias gespeichert; deine Ablehnung hält die Aufnahme angehalten. "
                        "Nur eine neue ausdrückliche Zustimmung per /alias erlaubt späteres "
                        "Fortsetzen."
                    )
                else:
                    text = (
                        "Alias gespeichert. Es gibt keine aktive Sitzung; deine Auswahl "
                        "wurde deshalb nicht als Aufnahmezustimmung gespeichert. Bitte /alias "
                        "während der wartenden Sitzung erneut ausfüllen."
                    )
            elif name == "alias-delete":
                text = "Dein Alias wurde gelöscht; die Aufzeichnungszustimmung wurde widerrufen."
            else:
                text = (
                    json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "OK"
                )
            if isinstance(result, dict) and name in (
                "setup",
                "show",
                "mode",
                "acl",
                "default_protocol",
                "protocol",
            ):
                result = {
                    "configuration": result,
                    "notice": "Aufnahme und Cloud bleiben bis zur Betriebsfreigabe gesperrt.",
                }
                text = json.dumps(result, ensure_ascii=False, indent=2)
            if len(text) <= 1800:
                await ctx.respond(
                    text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                data = text.encode()
                if len(data) > 2_000_000:
                    raise ValueError("response attachment exceeds bound")
                await ctx.respond(
                    file=discord.File(io.BytesIO(data), filename="sessionscribe.json"),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except ReceiveUnavailable:
            await ctx.respond(
                "Die Aufnahme ist bis zum geprüften DAVE-Empfang gesperrt. "
                "Native Hintergrundverarbeitung ist noch nicht freigeschaltet.",
                ephemeral=True,
            )
        except Denied:
            await ctx.respond("Berechtigung oder freigegebener Kanal fehlt.", ephemeral=True)
        except KeyError:
            await ctx.respond("Server mit /setup einrichten; Sitzungs-ID prüfen.", ephemeral=True)
        except Conflict:
            message = (
                "Dieser Alias ist im Sprachkanal bereits vergeben. Bitte wähle einen anderen."
                if name == "alias"
                else "Aktive Sitzung oder Verarbeitung zuerst abschließen."
            )
            await ctx.respond(message, ephemeral=True)
        except ValueError:
            await ctx.respond("Ungültige Eingabe oder Ergebnis nicht verfügbar.", ephemeral=True)
        except Exception:
            logging.getLogger(__name__).error("Command failed; details suppressed")
            await ctx.respond(
                "Befehl fehlgeschlagen; keine Details veröffentlicht.", ephemeral=True
            )

    async def run(self):
        self.loop = asyncio.get_running_loop()

        def initialize():
            def status(guild_id, channel_id, message):
                future = asyncio.run_coroutine_threadsafe(
                    self.send_status(guild_id, channel_id, message), self.loop
                )
                try:
                    return future.result(timeout=5)
                except Exception:
                    future.cancel()
                    return None

            def close_audio(_guild_id, _sid):
                return None

            self.service = ControlService(self.policy, status, close_audio)

        heartbeat = None
        try:
            token = discord_token(self.policy.token_file)
            await self.policy_call(initialize)
            self.health()
            heartbeat = asyncio.create_task(self.heartbeat())
            await self.client.start(token.get_secret_value())
        finally:
            self.ready = False
            with contextlib.suppress(OSError):
                self.health()
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError):
                    await heartbeat
            await self.client.close()
            if self.service:
                await self.policy_call(self.service.close)
            self.executor.shutdown(wait=True, cancel_futures=True)


def run(policy_file: Path):
    # Third-party debug/error logs can contain HTTP payloads. Keep those disabled;
    # our fixed messages deliberately omit exception text and interaction content.
    for name in ("discord", "aiohttp"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        policy = BotPolicy.model_validate_json(policy_file.read_bytes())
        asyncio.run(ControlBot(policy).run())
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit(
            "Control bot startup failed; check private policy, token and connectivity"
        ) from None
