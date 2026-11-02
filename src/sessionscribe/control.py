from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable

from .models import Actor, Config, InferenceSettings, Session, SessionState
from .store import Conflict, Store


class Denied(PermissionError):
    pass


class Controller:
    """Transport-independent slash-command policy. All identities are numeric IDs."""

    def __init__(
        self,
        config: Config,
        store: Store,
        status: Callable[[str, str], str],
        close_audio: Callable[[str], None],
    ):
        if config.tenant != store.tenant:
            raise ValueError("tenant mismatch")
        self.config, self.store = config, store
        self.status, self.close_audio = status, close_audio

    def authorize(self, actor: Actor, action: str):
        c = self.config
        if actor.guild_id != c.guild_id or actor.channel_id not in c.command_channel_ids:
            raise Denied("guild or command channel denied")
        grant = c.grants.get(action)
        if not grant or not (actor.user_id in grant.users or actor.role_ids & grant.roles):
            raise Denied("action denied")

    def participant(self, actor: Actor, session: Session):
        if (
            actor.guild_id != self.config.guild_id
            or actor.channel_id not in self.config.command_channel_ids
            or actor.user_id not in session.participants
        ):
            raise Denied("participant denied")

    def owned(self, actor: Actor, sid: str, action: str) -> Session:
        self.authorize(actor, action)
        s = self.store.session(sid)
        if actor.user_id != s.owner_id:
            raise Denied("session owner required")
        return s

    def start(
        self,
        actor: Actor,
        voice_channel_id: str,
        participants: frozenset[str],
        *,
        recorder_bot_id: str | None = None,
    ) -> Session:
        self.authorize(actor, "start")
        if not self.config.recording_enabled:
            raise Denied("recording disabled")
        if voice_channel_id not in self.config.voice_channel_ids:
            raise Denied("voice channel denied")
        if not participants or actor.user_id not in participants:
            raise Denied("owner must participate")
        if len(participants) > self.config.limits.max_participants:
            raise Denied("participant limit")
        if recorder_bot_id is not None and recorder_bot_id not in self.config.recorder_bot_ids:
            raise Denied("recording bot not allowed for this guild")
        # A bot identity can occupy one voice channel per guild. Different assigned
        # recording bots may capture different channels concurrently.

        for row in self.store.db.execute("SELECT id FROM sessions").fetchall():
            try:
                current = self.store.session(row[0])
            except KeyError:
                continue
            if current.state in (
                SessionState.WAITING,
                SessionState.RECORDING,
                SessionState.PAUSED,
            ) and (
                current.voice_channel_id == voice_channel_id
                or current.recorder_bot_id == recorder_bot_id
                or current.recorder_bot_id is None
                or recorder_bot_id is None
            ):
                raise Conflict("voice channel or recording bot already active in guild")
        s = Session(
            id=uuid.uuid4().hex,
            tenant=self.config.tenant,
            mode=self.config.mode,
            owner_id=actor.user_id,
            voice_channel_id=voice_channel_id,
            recorder_bot_id=recorder_bot_id,
            protocol_channel_id=self.config.protocol_channel(voice_channel_id),
            status_channel_id=self.config.output_channel_id,
            participants=participants,
            attendees=participants,
            inference=self.config.inference,
            characters=self.config.characters,
            campaign=self.config.campaign,
        )
        self.status(self.config.output_channel_id, "Warte auf ausdrückliche Aufnahmezustimmung.")
        self.store.add_session(s)
        return s

    def consent(self, actor: Actor, sid: str, *, cloud: bool) -> Session:
        s = self.store.session(sid)
        self.participant(actor, s)
        if s.state not in (SessionState.WAITING, SessionState.PAUSED):
            raise Conflict("consent requires a paused session")
        s2 = s.model_copy(
            update={
                "consented": s.consented | {actor.user_id},
                "cloud_consent": (
                    (s.cloud_consent | {actor.user_id})
                    if cloud
                    else s.cloud_consent - {actor.user_id}
                ),
            }
        )
        self.store.save_session(s, s2)
        return s2

    def resume(self, actor: Actor, sid: str) -> Session:
        s = self.owned(actor, sid, "resume")
        if s.state not in (SessionState.WAITING, SessionState.PAUSED):
            raise Conflict("session is not paused")
        if (
            not s.participants
            or not s.participants <= s.consented
            or len(s.attendees) > self.config.limits.max_participants
        ):
            raise Denied("missing participant consent")
        receipt = self.status(
            s.status_channel_id or self.config.output_channel_id,
            "Aufnahme läuft. /stop beendet sofort.",
        )
        if not receipt:
            raise Denied("visible recording status was not acknowledged")
        s2 = s.model_copy(update={"state": SessionState.RECORDING})
        self.store.save_session(s, s2)
        return s2

    def pause(self, actor: Actor, sid: str) -> Session:
        s = self.owned(actor, sid, "pause")
        if s.state != SessionState.RECORDING:
            raise Conflict("session is not recording")
        return self._halt(s, SessionState.PAUSED, "manual_pause")

    def stop(self, actor: Actor, sid: str) -> Session:
        s = self.store.session(sid)
        # Every current participant has a local emergency stop, independent of cloud/roles.
        try:
            self.participant(actor, s)
        except Denied:
            self.authorize(actor, "stop")
        if s.state not in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED):
            raise Conflict("session is already finalized")
        stopped = self._halt(s, SessionState.STOPPED, "explicit_stop")
        audio = self.store.files.root / "sessions" / sid / "audio"
        if audio.is_dir() and not audio.is_symlink() and next(audio.glob("*.flac"), None):
            self.store.enqueue(sid, "stt")
        return stopped

    def _halt(self, s: Session, state: SessionState, reason: str) -> Session:
        updated = s.model_copy(update={"state": state, "stop_reason": reason})
        self.store.save_session(s, updated)  # Close the write gate before any I/O/network.
        self.close_audio(s.id)
        # Status failure must never undo a local stop.
        with contextlib.suppress(OSError):
            self.status(
                s.status_channel_id or self.config.output_channel_id, "Aufnahme pausiert/beendet."
            )
        return updated

    def membership(self, sid: str, participants: frozenset[str]) -> Session:
        s = self.store.session(sid)
        if s.state not in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED):
            raise Conflict("session is finalized")
        added = participants - s.participants
        oversized = len(s.attendees | participants) > self.config.limits.max_participants
        changed = s.model_copy(
            update={
                "participants": participants,
                "attendees": s.attendees | participants,
                "consented": s.consented & participants,
                "cloud_consent": s.cloud_consent - added,
                "state": SessionState.PAUSED if added or oversized or not participants else s.state,
            }
        )
        self.store.save_session(s, changed)
        if added or oversized or not participants:
            self.close_audio(s.id)
        return changed

    def revoke(self, actor: Actor, sid: str):
        s = self.store.session(sid)
        if (
            actor.guild_id != self.config.guild_id
            or actor.channel_id not in self.config.command_channel_ids
            or actor.user_id not in s.attendees
        ):
            raise Denied("session attendee required")
        s2 = (
            self._halt(s, SessionState.PAUSED, "consent_revoked")
            if s.state == SessionState.RECORDING
            else s
        )
        self.store.save_session(
            s2,
            s2.model_copy(
                update={
                    "consented": s2.consented - {actor.user_id},
                    "cloud_consent": s2.cloud_consent - {actor.user_id},
                }
            ),
        )

    def process(self, actor: Actor, sid: str) -> str:
        self.owned(actor, sid, "process")
        return self.store.enqueue(sid, "stt")

    def restart_analysis(self, actor: Actor, sid: str, settings: InferenceSettings) -> str:
        self.owned(actor, sid, "process")
        if not self.config.cloud_enabled:
            raise Denied("cloud processing disabled")
        return self.store.restart_analysis(sid, settings)

    def result(self, actor: Actor, sid: str) -> bytes:
        self.owned(actor, sid, "result")
        return self.store.artifact(sid, "result.md")

    def status_for(self, actor: Actor):
        self.authorize(actor, "status")
        sessions = []
        for row in self.store.db.execute("SELECT id FROM sessions").fetchall():
            with contextlib.suppress(KeyError):
                s = self.store.session(row[0])
                if s.owner_id == actor.user_id:
                    sessions.append(
                        {
                            "session_id": s.id,
                            "state": s.state.value,
                            "participants": len(s.participants),
                            "voice_channel_id": s.voice_channel_id,
                            "recorder_bot_id": s.recorder_bot_id,
                            "protocol_channel_id": s.protocol_channel_id,
                        }
                    )
        protocols = []
        for item in self.store.protocol_queue():
            with contextlib.suppress(KeyError):
                if self.store.session(item["session_id"]).owner_id == actor.user_id:
                    protocols.append(item)
        return {"sessions": sessions, "queue": self.queue(actor), "protocol_outbox": protocols}

    def queue(self, actor: Actor):
        self.authorize(actor, "status")
        # Restrict queue visibility to the caller's sessions.
        visible = []
        for job in self.store.queue_status():
            with contextlib.suppress(KeyError):
                if self.store.session(job["session_id"]).owner_id == actor.user_id:
                    visible.append(job)
        return visible

    def delete(self, actor: Actor, sid: str):
        self.authorize(actor, "delete")
        row = self.store.db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return
        s = Session.model_validate_json(row[0])
        if s.owner_id != actor.user_id or s.tenant != self.config.tenant:
            raise Denied("session owner required")
        with self.store.files.lock(f"locks/{s.id}.lock") as acquired:
            if not acquired:
                raise Conflict("session operation must finish before deletion")
            with self.store.transaction():
                if self.store.db.execute(
                    "SELECT 1 FROM jobs WHERE session_id=? AND state='running'", (sid,)
                ).fetchone():
                    raise Conflict("processing must stop before deletion")
                if s.state not in (
                    SessionState.STOPPED,
                    SessionState.INTERRUPTED,
                    SessionState.DELETED,
                ):
                    raise Conflict("stop recording before deletion")
                job_ids = {
                    row[0]
                    for row in self.store.db.execute(
                        "SELECT id FROM jobs WHERE session_id=?", (sid,)
                    )
                }
                job_ids.update(
                    row[0]
                    for row in self.store.db.execute(
                        "SELECT id FROM retired_jobs WHERE session_id=?", (sid,)
                    )
                )
                self.store.db.execute("UPDATE jobs SET state='dead' WHERE session_id=?", (sid,))
                self.store.db.execute("DELETE FROM deliveries WHERE session_id=?", (sid,))
                self.store.save_session(s, s.model_copy(update={"state": SessionState.DELETED}))
            # A failed cleanup retains an inaccessible tombstone and is retryable.
            import re
            import shutil

            directories = [f"sessions/{s.id}"]
            # Include uncommitted token-specific files left by a crash before the DB commit.
            if any(not re.fullmatch(r"[a-f0-9]{32}", jid) for jid in job_ids):
                raise ValueError("invalid result job identity")
            with (
                contextlib.suppress(FileNotFoundError),
                self.store.files.parent("results/probe") as pair,
            ):
                import os

                fd, _ = pair
                for name in os.listdir(fd):
                    if re.fullmatch(r"[a-f0-9]{32}-[a-f0-9]{32}", name) and name[:32] in job_ids:
                        directories.append(f"results/{name}")
            for relative in directories:
                directory = self.store.files.root / relative
                with contextlib.suppress(FileNotFoundError), self.store.files.parent(relative):
                    if directory.is_symlink():
                        raise ValueError("symlink session/result directory")
                    if directory.exists():
                        shutil.rmtree(directory)

            with self.store.transaction():
                self.store.db.execute("DELETE FROM jobs WHERE session_id=?", (sid,))
                self.store.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
