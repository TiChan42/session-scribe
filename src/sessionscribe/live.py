"""Bounded handoff from SDK receive threads to the single recorder policy thread.

No Discord client, token or database is accessed by the receive callback. Pausing
closes the ingress immediately and invalidates frames still waiting in the queue.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from .models import Session
from .recorder import FRAME_BYTES, Frame
from .voice import RtpTimeline, RtpTimingDrop


@dataclass
class LiveSession:
    session: Session
    origin: float
    generation: int = 0
    enabled: threading.Event = field(default_factory=threading.Event)
    failure: str | None = None
    timing_drops: int = 0
    frames: int = 0
    nonzero_frames: int = 0
    speakers: dict[str, int] = field(default_factory=dict)


class AudioIngress:
    def __init__(self, capacity: int = 2048):
        self.frames = queue.Queue(maxsize=capacity)
        self.sessions: dict[tuple[str, str], LiveSession] = {}
        self.lock = threading.RLock()

    def activate(self, session: Session, origin: float) -> LiveSession:
        with self.lock:
            key = session.tenant, session.id
            live = self.sessions.get(key)
            if live is None:
                if len(self.sessions) >= 32:
                    raise ValueError("bounded live session capacity exceeded")
                live = self.sessions[key] = LiveSession(session, origin)
            live.session = session
            live.failure = None
            live.generation += 1
            live.enabled.set()
            return live

    def pause(self, guild: str, sid: str):
        with self.lock:
            live = self.sessions.get((guild, sid))
            if live:
                live.enabled.clear()
                live.generation += 1

    def fail(self, live: LiveSession, reason: str):
        with self.lock:
            live.failure = live.failure or reason
            live.enabled.clear()
            live.generation += 1

    def accept(self, live: LiveSession, frame: Frame):
        with self.lock:
            if not live.enabled.is_set():
                return
            try:
                self.frames.put_nowait((live, live.generation, frame))
            except queue.Full:
                self.fail(live, "audio_queue_full")

    def drain(self, limit=256):
        for _ in range(limit):
            try:
                live, generation, frame = self.frames.get_nowait()
            except queue.Empty:
                return
            with self.lock:
                valid = live.enabled.is_set() and generation == live.generation
            if valid:
                yield live, frame

    def forget(self, guild: str, sid: str):
        with self.lock:
            self.pause(guild, sid)
            self.sessions.pop((guild, sid), None)


def receive_sink(state, live: LiveSession, ingress: AudioIngress):
    """Accept only decrypted PCM attributed by the authenticated SDK SSRC map."""
    import discord

    timeline = RtpTimeline()

    class Sink(discord.sinks.Sink):
        def write(self, data, user):
            if not live.enabled.is_set():
                return
            try:
                uid = state.ssrc_user_map.get(data.packet.ssrc)
                if (
                    uid is None
                    or str(uid) not in live.session.participants
                    or user is not None
                    and user.id != uid
                ):
                    ingress.fail(live, "unapproved_voice_source")
                    return
                if not state.dave_session.ready or not data.packet.decrypted_data:
                    ingress.fail(live, "dave_not_ready")
                    return
                pcm = data.pcm
                if not pcm or len(pcm) % FRAME_BYTES or len(pcm) > 6 * FRAME_BYTES:
                    ingress.fail(live, "invalid_audio_frame")
                    return
                stamp = timeline.timestamp(
                    f"{uid}:{data.packet.ssrc}",
                    data.packet.timestamp,
                    int((time.monotonic() - live.origin) * 1000),
                )
                for offset in range(len(pcm) // FRAME_BYTES):
                    ingress.accept(
                        live,
                        Frame(
                            str(uid),
                            stamp + 20 * offset,
                            pcm[offset * FRAME_BYTES : (offset + 1) * FRAME_BYTES],
                        ),
                    )
            except RtpTimingDrop:
                live.timing_drops += 1
            except (AttributeError, TypeError, ValueError):
                ingress.fail(live, "invalid_audio_packet")

        def cleanup(self):
            if live.enabled.is_set():
                ingress.fail(live, "receiver_stopped")

    return Sink()
