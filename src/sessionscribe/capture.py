"""Bounded concurrent disk capture; one voice slot per recording bot and guild.

The recorder I/O loop owns this pool and its SQLite handles. The Discord transport
must supply authenticated bot/guild membership and bounded frame handoff queues.
Tokens and network clients are not part of this module.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass

from pydantic import Field

from .control import Controller, Denied
from .models import Actor, Limits, Session, SessionState, Snowflake, StrictModel
from .recorder import Frame, Recorder
from .store import Conflict, Store


class RecordingBot(StrictModel):
    id: Snowflake
    guild_ids: frozenset[Snowflake]


class CaptureBudget(StrictModel):
    max_sessions: int = Field(default=4, ge=1, le=32)
    max_reserved_tracks: int = Field(default=32, ge=1, le=256)
    max_reserved_audio_bytes: int = Field(default=4 * 1024**3, ge=8192)
    min_free_bytes: int = Field(default=512 * 1024**2, ge=0)


@dataclass
class Capture:
    recorder: Recorder
    bot_id: str
    limits: Limits


class RecordingPool:
    def __init__(self, bots: tuple[RecordingBot, ...], budget: CaptureBudget | None = None):
        if len({bot.id for bot in bots}) != len(bots):
            raise ValueError("duplicate recording bot identity")
        self.bots, self.budget = bots, budget or CaptureBudget()
        self._captures: dict[tuple[str, str], Capture] = {}
        self._lock = threading.RLock()

    def start(
        self,
        controller: Controller,
        actor: Actor,
        voice_channel_id: str,
        participants: frozenset[str],
        *,
        speaker_aliases: dict[str, str] | None = None,
    ) -> Session:
        with self._lock:
            controller.authorize(actor, "start")
            limits = controller.config.limits
            used_tracks = sum(c.limits.max_participants for c in self._captures.values())
            used_audio = sum(c.limits.max_audio_bytes for c in self._captures.values())
            if (
                len(self._captures) >= self.budget.max_sessions
                or used_tracks + limits.max_participants > self.budget.max_reserved_tracks
                or used_audio + limits.max_audio_bytes > self.budget.max_reserved_audio_bytes
            ):
                raise Conflict("global recording capacity exhausted")
            # Keep enough free space for still-unused reservations on this filesystem.
            device = controller.store.files.root.stat().st_dev
            outstanding = sum(
                max(0, c.limits.max_audio_bytes - c.recorder.bytes_written)
                for c in self._captures.values()
                if c.recorder.store.files.root.stat().st_dev == device
            )
            if shutil.disk_usage(controller.store.files.root).free < (
                self.budget.min_free_bytes + outstanding + limits.max_audio_bytes
            ):
                raise Conflict("insufficient reserved recording storage")
            occupied = {(c.bot_id, guild) for (guild, _sid), c in self._captures.items()}
            selected = next(
                (
                    bot
                    for bot in self.bots
                    if actor.guild_id in bot.guild_ids
                    and bot.id in controller.config.recorder_bot_ids
                    and (bot.id, actor.guild_id) not in occupied
                ),
                None,
            )
            if selected is None:
                raise Conflict("no free recording bot in this guild")
            session = controller.start(
                actor,
                voice_channel_id,
                participants,
                recorder_bot_id=selected.id,
                speaker_aliases=speaker_aliases,
            )
            try:
                capture_store = Store(controller.store.files.root, actor.guild_id)
                recorder = Recorder(capture_store, session.id, limits)
            except Exception:
                if "capture_store" in locals():
                    capture_store.close()
                controller.store.save_session(
                    session,
                    session.model_copy(
                        update={
                            "state": SessionState.INTERRUPTED,
                            "stop_reason": "recorder_start_failed",
                        }
                    ),
                )
                raise
            self._captures[(actor.guild_id, session.id)] = Capture(recorder, selected.id, limits)
            return session

    def accept(self, bot_id: str, guild_id: str, sid: str, frame: Frame) -> bool:
        with self._lock:
            capture = self._captures.get((guild_id, sid))
            if capture is None or capture.bot_id != bot_id:
                raise Denied("frame does not belong to this bot/guild/session")
            accepted = capture.recorder.accept(frame)
            if capture.recorder.failure:
                self.close_session(guild_id, sid)
            return accepted

    def close_session(self, guild_id: str, sid: str):
        with self._lock:
            capture = self._captures.get((guild_id, sid))
            if capture is None:
                return
            capture.recorder.close()
            session = capture.recorder.store.session(sid)
            if session.state in (SessionState.STOPPED, SessionState.INTERRUPTED):
                if capture.recorder.chunk_count:
                    capture.recorder.store.enqueue(sid, "stt")
                capture.recorder.store.close()
                self._captures.pop((guild_id, sid))

    def tick(self, guild_id: str, sid: str, elapsed_ms: int):
        with self._lock:
            capture = self._captures[(guild_id, sid)]
            capture.recorder.tick(elapsed_ms)
            if capture.recorder.failure:
                self.close_session(guild_id, sid)

    def status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "guild_id": guild,
                    "session_id": sid,
                    "recorder_bot_id": c.bot_id,
                    "reserved_tracks": c.limits.max_participants,
                    "audio_bytes_accounted": c.recorder.bytes_written,
                    "chunks": c.recorder.chunk_count,
                }
                for (guild, sid), c in self._captures.items()
            ]

    def interrupt_all(self):
        """Local shutdown/disconnect: retain audio, queue closed parts, never auto-resume."""
        with self._lock:
            errors = []
            for (guild, sid), capture in list(self._captures.items()):
                try:
                    session = capture.recorder.store.session(sid)
                    capture.recorder.failure = "recorder_shutdown"
                    capture.recorder.store.save_session(
                        session,
                        session.model_copy(
                            update={
                                "state": SessionState.INTERRUPTED,
                                "stop_reason": "recorder_shutdown",
                            }
                        ),
                    )
                    self.close_session(guild, sid)
                except OSError as error:
                    errors.append(error)
            if errors:
                raise OSError("one or more recordings could not be finalized") from None
