"""Explicit single-participant DAVE qualification; never enables production voice."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from .credentials import discord_token
from .models import Actor, Config, Grant, InferenceSettings, Limits, Snowflake, StrictModel
from .voice import RtpTimeline


class TestPolicy(StrictModel):
    __test__ = False
    purpose: Literal["authorized-single-person-dave-test"]
    guild_id: Snowflake
    voice_channel_id: Snowflake
    protocol_channel_id: Snowflake
    participant_id: Snowflake
    expires_at: int
    cloud_consent: bool
    duration_seconds: int = Field(default=60, ge=15, le=60)
    inference: InferenceSettings
    revision: str = Field(default="development", pattern=r"^(development|[a-f0-9]{40})$")


class DiskCapture(threading.Thread):
    def __init__(self, policy, visible, root=Path("/data/voice-tests")):
        super().__init__(name="bounded-test-disk-writer", daemon=False)
        self.policy, self.visible = policy, visible
        self.root = root
        self.frames = queue.Queue(maxsize=256)
        self.stop_requested, self.ready = threading.Event(), threading.Event()
        self.failure = None
        self.receipt = None
        self.started_at = None
        self.frame_count = self.nonzero_frames = 0

    def accept(self, frame):
        if self.stop_requested.is_set() or not self.ready.is_set():
            return
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            self.abort("bounded_audio_queue_full")

    def abort(self, reason):
        self.failure = self.failure or reason
        self.stop_requested.set()

    def run(self):
        from .control import Controller
        from .recorder import Recorder
        from .store import Store

        policy, holder = self.policy, {}
        store = None
        try:
            root = self.root / uuid.uuid4().hex
            config = Config(
                mode="meeting",
                guild_id=policy.guild_id,
                command_channel_ids=frozenset({policy.protocol_channel_id}),
                voice_channel_ids=frozenset({policy.voice_channel_id}),
                output_channel_id=policy.protocol_channel_id,
                default_protocol_channel_id=policy.protocol_channel_id,
                grants={
                    action: Grant(users=frozenset({policy.participant_id}))
                    for action in ("start", "resume", "stop")
                },
                inference=policy.inference,
                recording_enabled=True,
                cloud_enabled=policy.cloud_consent,
                limits=Limits(
                    max_participants=1,
                    max_duration_ms=policy.duration_seconds * 1000,
                    max_audio_bytes=32 * 1024**2,
                    min_free_bytes=512 * 1024**2,
                ),
            )
            store = Store(root, policy.guild_id)
            store.files.json("test-config.json", config.model_dump(mode="json"))
            store.files.json("test-authorization.json", policy.model_dump(mode="json"))
            controller = Controller(
                config, store, self.visible, lambda _sid: holder["recorder"].close()
            )
            actor = Actor(
                guild_id=policy.guild_id,
                channel_id=policy.protocol_channel_id,
                user_id=policy.participant_id,
            )
            session = controller.start(
                actor, policy.voice_channel_id, frozenset({policy.participant_id})
            )
            holder["recorder"] = Recorder(store, session.id, config.limits)
            # This separate operator test accepts only the private expiring policy
            # bound to the participant's explicit consent, never a guild command.
            controller.consent(actor, session.id, cloud=policy.cloud_consent)
            controller.resume(actor, session.id)
            self.started_at = time.monotonic()
            self.ready.set()
            while not self.stop_requested.is_set():
                if time.monotonic() - self.started_at >= policy.duration_seconds:
                    self.stop_requested.set()
                    break
                try:
                    frame = self.frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if holder["recorder"].accept(frame):
                    self.frame_count += 1
                    self.nonzero_frames += any(frame.pcm)
                if holder["recorder"].failure:
                    self.abort(holder["recorder"].failure)
            controller.stop(actor, session.id)
            self.receipt = {
                "schema": 1,
                "session_id": session.id,
                "root": str(root),
                "frames": self.frame_count,
                "nonzero_frames": self.nonzero_frames,
                "audio_chunks": holder["recorder"].chunk_count,
                "failure": self.failure,
                "dave_single_speaker_receive": self.nonzero_frames > 0 and not self.failure,
                "production_voice_qualified": False,
            }
            store.files.json("test-receipt.json", self.receipt)
        except Exception:
            self.abort("test_disk_writer_failed")
        finally:
            self.ready.set()
            if "recorder" in holder:
                with contextlib.suppress(OSError):
                    holder["recorder"].close()
            if store:
                store.close()


async def authorize_transport(payload, expiry):
    reader, writer = await asyncio.open_unix_connection("/voice-gate/udp.sock", limit=2048)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        async with asyncio.timeout(10):
            reply = json.loads(await reader.readline())
        expected = (
            {"authorized", "expires_in", "udp_ticket"}
            if payload.get("transport") == "wss"
            else {"authorized", "expires_in"}
        )
        if (
            set(reply) != expected
            or reply.get("authorized") is not True
            or reply.get("expires_in") != expiry
        ):
            raise PermissionError("negotiated Discord transport target denied")
        if "udp_ticket" in expected and (
            not isinstance(reply["udp_ticket"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", reply["udp_ticket"])
        ):
            raise PermissionError("bounded voice connection ticket required")
        return reply.get("udp_ticket")
    finally:
        writer.close()
        await writer.wait_closed()


async def test(policy):
    import discord
    from discord.voice import VoiceClient
    from discord.voice.gateway import VoiceWebSocket
    from discord.voice.receive.reader import UDPKeepAlive

    if (
        UDPKeepAlive.delay != 5
        or "g10a5e8cf13bd8db21f95baf62cf866056d1e1091" not in discord.__version__
    ):
        raise ValueError("reviewed pinned receive candidate required")
    if not time.time() < policy.expires_at <= time.time() + 600:
        raise PermissionError("fresh explicit participant authorization required")
    original = VoiceWebSocket.ready
    original_factory = VoiceWebSocket.from_state
    original_factory_descriptor = VoiceWebSocket.__dict__["from_state"]
    tickets = {}

    async def approved_factory(_cls, state, **kwargs):
        endpoint = urlsplit("//" + state.endpoint)
        tickets[id(state)] = await authorize_transport(
            {"transport": "wss", "hostname": endpoint.hostname, "port": endpoint.port or 443},
            180,
        )
        return await original_factory(state, **kwargs)

    async def approved_ready(ws, data):
        await authorize_transport(
            {
                "hostname": urlsplit("//" + ws.state.endpoint).hostname,
                "ip": data["ip"],
                "port": data["port"],
                "ticket": tickets.get(id(ws.state)),
            },
            90,
        )
        await original(ws, data)

    VoiceWebSocket.ready = approved_ready
    VoiceWebSocket.from_state = classmethod(approved_factory)
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    stop = asyncio.Event()
    capture = voice = login = None
    phase = "login"
    loop = asyncio.get_running_loop()
    channel = None

    def membership_ok():
        return channel is not None and set(channel.voice_states) - {client.user.id} == {
            int(policy.participant_id)
        }

    @client.event
    async def on_voice_state_update(_member, before, after):
        if (
            capture is not None
            and _member.id == client.user.id
            and before.channel == channel
            and after.channel != channel
        ):
            capture.abort("voice_disconnected")
            stop.set()
        if (
            capture is not None
            and (before.channel == channel or after.channel == channel)
            and not membership_ok()
        ):
            capture.abort("voice_membership_changed")
            stop.set()

    @client.event
    async def on_disconnect():
        if capture is not None:
            capture.abort("gateway_disconnected")
            stop.set()

    class StopView(discord.ui.View):
        @discord.ui.button(label="Testaufnahme stoppen", style=discord.ButtonStyle.danger)
        async def button(self, _button, interaction):
            if (
                str(interaction.user.id) != policy.participant_id
                or str(interaction.guild_id) != policy.guild_id
            ):
                await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
                return
            if capture is not None:
                capture.stop_requested.set()
            stop.set()
            await interaction.response.send_message("Testaufnahme beendet.", ephemeral=True)

    try:
        token = discord_token(Path("/run/secrets/bot.env"))
        login = asyncio.create_task(client.start(token.get_secret_value()))
        async with asyncio.timeout(60):
            await client.wait_until_ready()
        phase = "guild_access"
        guild = client.get_guild(int(policy.guild_id))
        if guild is None:
            raise PermissionError("approved test guild unavailable")
        channel = guild.get_channel(int(policy.voice_channel_id))
        output = guild.get_channel(int(policy.protocol_channel_id))
        if not isinstance(channel, discord.VoiceChannel) or not isinstance(
            output, discord.TextChannel
        ):
            raise PermissionError("approved same-guild channels required")
        phase = "waiting_participant"
        for _ in range(120):
            if membership_ok():
                break
            await asyncio.sleep(1)
        if not membership_ok():
            raise PermissionError("only the consenting participant may attend")
        phase = "voice_connect"

        class MutedVoiceClient(VoiceClient):
            async def connect(self, *, reconnect, timeout):
                await super().connect(
                    reconnect=reconnect, timeout=timeout, self_deaf=False, self_mute=True
                )

        voice = await channel.connect(cls=MutedVoiceClient, timeout=30, reconnect=False)
        state = voice._connection
        phase = "dave_readiness"
        # The SDK marks voice connected after transport keys arrive. MLS/DAVE
        # commits arrive later on its ongoing websocket poll task.
        async with asyncio.timeout(15):
            while state.dave_session is None or not state.dave_session.ready:
                if not membership_ok() or not voice.is_connected():
                    raise PermissionError("test membership or voice connection lost")
                await asyncio.sleep(0.1)
        if (
            state.dave_protocol_version < 1
            or state.dave_session is None
            or not state.dave_session.ready
        ):
            raise PermissionError("active DAVE encryption required")
        view = StopView(timeout=policy.duration_seconds + 15)

        def visible(_channel, _text):
            if _text.startswith("Aufnahme läuft"):
                text = (
                    "Autorisierte DAVE-Testaufnahme startet jetzt, höchstens "
                    f"{policy.duration_seconds} Sekunden. "
                    "Stopp: Knopf oder Sprachkanal verlassen."
                )
                kwargs = {"view": view}
            elif _text.startswith("Warte"):
                text = "Einzeltest vorbereitet; ausdrückliche Aufnahmezustimmung bestätigt."
                kwargs = {}
            else:
                text = "Testaufnahme beendet. Die Verarbeitung erfolgt anschließend."
                kwargs = {}
            future = asyncio.run_coroutine_threadsafe(
                output.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs), loop
            )
            return str(future.result(timeout=15).id)

        phase = "capture_start"
        capture = DiskCapture(policy, visible)
        capture.start()
        await asyncio.to_thread(capture.ready.wait, 20)
        if capture.failure or not capture.ready.is_set() or not membership_ok():
            raise RuntimeError("test start refused")
        timeline = RtpTimeline()

        class Sink(discord.sinks.Sink):
            def write(self, data, user):
                from .recorder import FRAME_BYTES, Frame

                if capture.stop_requested.is_set():
                    return
                uid = state.ssrc_user_map.get(data.packet.ssrc)
                if (
                    str(uid) != policy.participant_id
                    or user is not None
                    and user.id != uid
                    or not membership_ok()
                ):
                    capture.abort("unapproved_voice_source")
                    return
                if not state.dave_session.ready or not data.packet.decrypted_data:
                    capture.abort("dave_not_ready")
                    return
                pcm = data.pcm
                if not pcm or len(pcm) % FRAME_BYTES or len(pcm) > FRAME_BYTES * 6:
                    capture.abort("unexpected_audio_frame")
                    return
                try:
                    stamp = timeline.timestamp(
                        str(data.packet.ssrc),
                        data.packet.timestamp,
                        int((time.monotonic() - capture.started_at) * 1000),
                    )
                    for index in range(len(pcm) // FRAME_BYTES):
                        capture.accept(
                            Frame(
                                policy.participant_id,
                                stamp + 20 * index,
                                pcm[index * FRAME_BYTES : (index + 1) * FRAME_BYTES],
                            )
                        )
                except Exception:
                    capture.abort("invalid_audio_timeline")

            def cleanup(self):
                if not capture.stop_requested.is_set():
                    capture.abort("receiver_stopped_unexpectedly")
                capture.stop_requested.set()

        voice.start_recording(Sink())
        phase = "receiving"
        while not stop.is_set() and not capture.stop_requested.is_set():
            await asyncio.sleep(0.1)
        if voice.is_recording():
            voice.stop_recording()
        capture.stop_requested.set()
        await asyncio.to_thread(capture.join, 20)
        if capture.is_alive() or capture.receipt is None:
            raise RuntimeError("test finalization failed")
        print(json.dumps(capture.receipt))
        return capture.receipt
    except Exception as error:
        from .files import SafeFiles

        SafeFiles(Path("/data/voice-tests")).json(
            "test-phase.json", {"phase": phase, "error_type": type(error).__name__}
        )
        raise
    finally:
        if capture is not None:
            capture.stop_requested.set()
        if voice is not None:
            with contextlib.suppress(Exception):
                await voice.disconnect(force=True)
        await client.close()
        if login:
            login.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await login
        if capture is not None and capture.is_alive():
            await asyncio.to_thread(capture.join, 20)
        VoiceWebSocket.ready = original
        VoiceWebSocket.from_state = original_factory_descriptor


def run(path):
    for name in ("discord", "aiohttp"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        policy = TestPolicy.model_validate_json(path.read_bytes())
        asyncio.run(test(policy))
    except Exception:
        raise SystemExit("Authorized voice test failed; inspect private phase evidence") from None


def manual_actor(policy, ctx):
    from .bot import authenticated_actor
    from .control import Denied

    actor = authenticated_actor(ctx)
    actor.authorize_setup()
    if (
        actor.guild_id != policy.guild_id
        or actor.user_id != policy.participant_id
        or actor.channel_id != policy.protocol_channel_id
    ):
        raise Denied("operator test scope does not match the authenticated interaction")
    return actor


async def listen(policy):
    import discord

    from .control import Denied
    from .files import SafeFiles

    if policy.expires_at != 0:
        raise ValueError("manual listener needs a static non-recording policy")
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Bot(
        intents=intents, auto_sync_commands=False, allowed_mentions=discord.AllowedMentions.none()
    )
    files = SafeFiles(Path("/data/voice-tests"))
    active = asyncio.Lock()
    ready = False

    def health():
        files.json(
            "listener-health.json",
            {
                "schema": 1,
                "revision": policy.revision,
                "checked_at": time.time(),
                "command_ready": ready,
                "test_active": active.locked(),
            },
        )

    @client.slash_command(
        name="testaufnahme",
        description="60 Sekunden Einzeltest: Aufnahme und bestätigte Cloud-Textauswertung",
        guild_ids=[int(policy.guild_id)],
        default_member_permissions=discord.Permissions(manage_guild=True),
    )
    async def command(ctx):
        await ctx.defer(ephemeral=True)
        try:
            manual_actor(policy, ctx)
            if active.locked():
                await ctx.respond("Eine Testaufnahme läuft bereits.", ephemeral=True)
                return
            async with active:
                health()
                await ctx.respond(
                    "Einzeltest wird gestartet. Bleibe allein im freigegebenen Sprachkanal "
                    "und lies erst nach der sichtbaren Startmeldung vor.",
                    ephemeral=True,
                )
                runtime = policy.model_copy(update={"expires_at": int(time.time()) + 600})
                receipt = await test(runtime)
                await ctx.respond(
                    "Test beendet. Audio wurde lokal gespeichert und zur Verarbeitung eingereiht."
                    if receipt["dave_single_speaker_receive"]
                    else "Test beendet; noch kein verwertbarer DAVE-Audioempfang bestätigt.",
                    ephemeral=True,
                )
        except Denied:
            await ctx.respond(
                "Dieser Einzeltest ist nur für den freigegebenen Betreiber "
                "im Test-Protokollkanal erlaubt.",
                ephemeral=True,
            )
        except Exception:
            await ctx.respond(
                "Test konnte nicht abgeschlossen werden. Vorhandene Daten bleiben erhalten; "
                "Details bleiben privat.",
                ephemeral=True,
            )
        finally:
            health()

    @client.event
    async def on_ready():
        nonlocal ready
        try:
            async with asyncio.timeout(30):
                value = await client.http.upsert_guild_command(
                    client.application_id, int(policy.guild_id), command.to_dict()
                )
                command.id = value["id"]
                client._application_commands[command.id] = command
                commands = await client.http.get_guild_commands(
                    client.application_id, int(policy.guild_id)
                )
                ready = any(c["id"] == command.id and c["name"] == "testaufnahme" for c in commands)
        except Exception:
            ready = False
        health()

    @client.event
    async def on_disconnect():
        nonlocal ready
        ready = False
        health()

    @client.event
    async def on_error(_event, *_args, **_kwargs):
        logging.getLogger(__name__).error("Manual test event failed; details suppressed")

    async def heartbeat():
        while True:
            health()
            await asyncio.sleep(15)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await client.start(discord_token(Path("/run/secrets/bot.env")).get_secret_value())
    finally:
        ready = False
        health()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await client.close()


def run_listener(path):
    for name in ("discord", "aiohttp"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        asyncio.run(listen(TestPolicy.model_validate_json(path.read_bytes())))
    except Exception:
        raise SystemExit(
            "Manual test listener unavailable; no automatic recording started"
        ) from None
