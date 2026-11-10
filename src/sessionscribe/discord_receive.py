"""Pinned DAVE receiver and private recorder clients; no engine or model access."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import re
import time
from urllib.parse import urlsplit

from .credentials import discord_token
from .live import receive_sink
from .voice import ReceiveUnavailable


class DiscordReceiver:
    def __init__(self, bot, ingress):
        self.bot, self.ingress = bot, ingress
        self.clients, self.helpers, self.voices, self.tickets = {}, [], {}, {}
        self.originals = None
        self.locks = {}
        self.current_key = contextvars.ContextVar("voice_session_key", default=None)
        self.ticket_keys = {}
        self.connecting = set()
        self.disconnect_tasks = {}
        self.closing = False

    async def request(self, payload, expiry):
        reader, writer = await asyncio.open_unix_connection(self.bot.policy.voice_gate, limit=2048)
        try:
            async with asyncio.timeout(10):
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                raw = await reader.readline()
            if len(raw) > 2048:
                raise ReceiveUnavailable("voice_gateway_denied")
            reply = json.loads(raw)
            fields = {"authorized", "expires_in"}
            if payload.get("transport") == "wss":
                fields.add("udp_ticket")
            if (
                set(reply) != fields
                or reply.get("authorized") is not True
                or reply.get("expires_in") != expiry
            ):
                raise ReceiveUnavailable("voice_gateway_denied")
            if "udp_ticket" in fields and not re.fullmatch(
                "[a-f0-9]{64}", reply.get("udp_ticket", "")
            ):
                raise ReceiveUnavailable("voice_gateway_denied")
            return reply.get("udp_ticket")
        finally:
            writer.close()
            await writer.wait_closed()

    def install(self):
        import discord
        from discord.voice.gateway import VoiceWebSocket
        from discord.voice.receive.reader import UDPKeepAlive

        if (
            "g10a5e8cf13bd8db21f95baf62cf866056d1e1091" not in discord.__version__
            or UDPKeepAlive.delay != 5
        ):
            raise ReceiveUnavailable("adapter_not_pinned")
        if self.originals:
            return
        ready, factory = VoiceWebSocket.ready, VoiceWebSocket.from_state
        self.originals = ready, VoiceWebSocket.__dict__["from_state"]

        async def approved_factory(_cls, state, **kwargs):
            if self.closing:
                raise ReceiveUnavailable("recorder_shutdown")
            bound_key = self.current_key.get() or self.ticket_keys.get(id(state))
            if bound_key is None:
                raise ReceiveUnavailable("voice_not_authorized")
            if id(state) not in self.tickets and len(self.tickets) >= 32:
                raise ReceiveUnavailable("voice_capacity")
            old = self.tickets.pop(id(state), None)
            if old:
                with contextlib.suppress(Exception):
                    await self.request({"transport": "revoke", "ticket": old}, 0)
            endpoint = urlsplit("//" + state.endpoint)
            self.tickets[id(state)] = await self.request(
                {"transport": "wss", "hostname": endpoint.hostname, "port": endpoint.port or 443},
                180,
            )
            self.ticket_keys[id(state)] = bound_key
            return await factory(state, **kwargs)

        async def approved_ready(ws, data):
            await self.request(
                {
                    "hostname": urlsplit("//" + ws.state.endpoint).hostname,
                    "ip": data["ip"],
                    "port": data["port"],
                    "ticket": self.tickets.get(id(ws.state)),
                },
                90,
            )
            await ready(ws, data)

        VoiceWebSocket.ready = approved_ready
        VoiceWebSocket.from_state = classmethod(approved_factory)

    async def start_helpers(self):
        import discord

        self.install()
        for credential in self.bot.policy.recording_bots:
            intents = discord.Intents.none()
            intents.guilds = intents.voice_states = True
            client = discord.Client(
                intents=intents, allowed_mentions=discord.AllowedMentions.none()
            )

            async def lost(client=client):
                self.pause_bot(str(client.user.id) if client.user else "")

            async def changed(member, before, after, client=client):
                if (
                    client.user
                    and member.id == client.user.id
                    and before.channel is not None
                    and before.channel != after.channel
                ):
                    self.pause_bot(str(client.user.id))

            lost.__name__ = "on_disconnect"
            changed.__name__ = "on_voice_state_update"
            client.event(lost)
            client.event(changed)
            task = asyncio.create_task(
                client.start(discord_token(credential.token_file).get_secret_value())
            )
            self.helpers.append((client, task))
            async with asyncio.timeout(45):
                await client.wait_until_ready()
            if str(client.user.id) != credential.bot_id:
                raise ReceiveUnavailable("recorder_identity_mismatch")
            self.clients[credential.bot_id] = client

    async def ready(self):
        from .capture import RecordingBot

        if not self.bot.client.user:
            raise ReceiveUnavailable("recorder_unavailable")
        own = str(self.bot.client.user.id)
        if own in self.clients and self.clients[own] is not self.bot.client:
            raise ReceiveUnavailable("duplicate_recorder_identity")
        self.clients[own] = self.bot.client
        bots = []
        for bid, client in self.clients.items():
            allowed = self.bot.policy.recording_guild_ids
            configured = next((r for r in self.bot.policy.recording_bots if r.bot_id == bid), None)
            if configured:
                allowed &= configured.guild_ids
            guilds = frozenset(str(g.id) for g in client.guilds) & allowed
            if guilds:
                bots.append(RecordingBot(id=bid, guild_ids=guilds))
        await self.bot.policy_call(
            self.bot.service.set_recording_bots, tuple(sorted(bots, key=lambda b: b.id))
        )

    def pause_bot(self, bot_id):
        for live in list(self.ingress.sessions.values()):
            if live.session.recorder_bot_id == bot_id:
                self.ingress.fail(live, "voice_disconnected")

    def pause_channel(self, guild_id, channel_id):
        for key, live in list(self.ingress.sessions.items()):
            if live.session.tenant == guild_id and live.session.voice_channel_id == channel_id:
                self.ingress.pause(*key)

    def halt(self, guild_id, sid):
        self.ingress.pause(guild_id, sid)
        self.bot.loop.call_soon_threadsafe(self.schedule_disconnect, (guild_id, sid))

    def schedule_disconnect(self, key):
        task = self.disconnect_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._disconnect(key))
            self.disconnect_tasks[key] = task

            def completed(done):
                if self.disconnect_tasks.get(key) is done:
                    self.disconnect_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(completed)
        return task

    async def disconnect(self, key):
        await asyncio.shield(self.schedule_disconnect(key))

    async def connect(self, session, origin):
        if (
            self.bot.policy.voice_reception == "disabled"
            or session.tenant not in self.bot.policy.recording_guild_ids
            or session.state.value != "recording"
            or not session.participants <= session.consented
            or not session.participants <= session.speaker_aliases.keys()
        ):
            raise ReceiveUnavailable("voice_not_authorized")
        import discord
        from discord.voice import VoiceClient

        key = session.tenant, session.id
        if self.closing:
            raise ReceiveUnavailable("recorder_shutdown")
        if key in self.voices or key in self.disconnect_tasks:
            await self.disconnect(key)
        for old, old_lock in list(self.locks.items()):
            if not old_lock.locked() and old not in self.connecting and old not in self.voices:
                self.locks.pop(old, None)
        if key not in self.locks and len(self.locks) >= 64:
            raise ReceiveUnavailable("voice_capacity")
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self.voices:
                raise ReceiveUnavailable("voice_already_connected")
            client = self.clients.get(session.recorder_bot_id)
            guild = client.get_guild(int(session.tenant)) if client else None
            channel = guild.get_channel(int(session.voice_channel_id)) if guild else None
            if not isinstance(channel, discord.VoiceChannel):
                raise ReceiveUnavailable("recorder_guild_unavailable")

            def membership_ok():
                current = frozenset(str(m.id) for m in channel.members if not m.bot)
                return current == session.participants

            if not membership_ok():
                raise ReceiveUnavailable("voice_membership_changed")
            live = self.ingress.activate(
                session, origin if origin is not None else time.monotonic()
            )
            generation = live.generation
            # Admission is closed during connection; no audio before DAVE readiness.
            live.enabled.clear()
            voice = None
            context = self.current_key.set(key)
            self.connecting.add(key)
            try:

                class MutedVoiceClient(VoiceClient):
                    async def connect(self, *, reconnect, timeout):
                        await super().connect(
                            reconnect=reconnect, timeout=timeout, self_deaf=False, self_mute=True
                        )

                voice = await channel.connect(cls=MutedVoiceClient, timeout=30, reconnect=False)
                state = voice._connection
                self.voices[key] = voice
                async with asyncio.timeout(15):
                    while state.dave_session is None or not state.dave_session.ready:
                        if not membership_ok() or not voice.is_connected():
                            raise ReceiveUnavailable("voice_membership_changed")
                        await asyncio.sleep(0.1)
                current = await self.bot.policy_call(self.bot.service.capture_session, *key)
                if (
                    current.state.value != "recording"
                    or not membership_ok()
                    or state.dave_protocol_version < 1
                ):
                    raise ReceiveUnavailable("voice_membership_changed")
                # A membership event during the await invalidates this generation.
                if live.generation != generation:
                    raise ReceiveUnavailable("voice_membership_changed")
                receipt = await self.bot.send_status(
                    session.tenant,
                    session.status_channel_id,
                    f"Aufnahme läuft jetzt. Sitzung `{session.id}`. "
                    "/pause pausiert, /stop beendet sofort.",
                )
                if not receipt:
                    raise ReceiveUnavailable("visible_start_missing")
                origin = await self.bot.policy_call(self.bot.service.capture_ready, *key)
                if live.generation != generation or not membership_ok():
                    raise ReceiveUnavailable("voice_membership_changed")
                live.origin = origin
                live.enabled.set()
                voice.start_recording(receive_sink(state, live, self.ingress))
            except BaseException:
                self.ingress.pause(*key)
                await self.disconnect(key)
                raise
            finally:
                self.connecting.discard(key)
                self.current_key.reset(context)

    async def _disconnect(self, key):
        self.ingress.pause(*key)
        voice = self.voices.pop(key, None)
        if voice:
            state = voice._connection
            self.ticket_keys.setdefault(id(state), key)
            with contextlib.suppress(Exception):
                async with asyncio.timeout(10):
                    if voice.is_recording():
                        voice.stop_recording()
                    await voice.disconnect(force=True)
        for state_id, bound in list(self.ticket_keys.items()):
            if bound == key:
                self.ticket_keys.pop(state_id, None)
                ticket = self.tickets.pop(state_id, None)
                if ticket:
                    with contextlib.suppress(Exception):
                        await self.request({"transport": "revoke", "ticket": ticket}, 0)

    async def renew(self):
        for ticket in tuple(self.tickets.values()):
            await self.request({"transport": "renew", "ticket": ticket}, 90)

    async def reconcile(self, active):
        for key in list(self.voices):
            if key in self.connecting:
                continue
            live = self.ingress.sessions.get(key)
            if live and not self.voices[key].is_connected():
                self.ingress.fail(live, "voice_disconnected")
            if key not in active or live is None or not live.enabled.is_set():
                self.schedule_disconnect(key)

    async def close(self):
        self.closing = True
        for key in list(self.voices):
            self.schedule_disconnect(key)
        if self.disconnect_tasks:
            await asyncio.gather(*tuple(self.disconnect_tasks.values()), return_exceptions=True)
        for client, task in self.helpers:
            await client.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.originals:
            from discord.voice.gateway import VoiceWebSocket

            VoiceWebSocket.ready, VoiceWebSocket.from_state = self.originals
            self.originals = None
