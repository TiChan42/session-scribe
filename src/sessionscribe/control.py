from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable

from .aliases import validate_alias
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
        speaker_aliases: dict[str, str] | None = None,
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
        aliases = {
            user_id: validate_alias(alias)
            for user_id, alias in (speaker_aliases or {}).items()
            if user_id in participants
        }
        if len({alias.casefold() for alias in aliases.values()}) != len(aliases):
            raise Conflict("voice aliases must be unique")
        s = Session(
            id=uuid.uuid4().hex,
            tenant=self.config.tenant,
            mode=self.config.mode,
            owner_id=actor.user_id,
            voice_channel_id=voice_channel_id,
            recorder_bot_id=recorder_bot_id,
            protocol_channel_id=self.config.protocol_channel(voice_channel_id),
            status_channel_id=actor.channel_id,
            participants=participants,
            attendees=participants,
            speaker_aliases=aliases,
            inference=self.config.inference,
            characters=self.config.characters,
            campaign=self.config.campaign,
            stop_reason="consent_required",
        )
        self.store.add_session(s)
        with contextlib.suppress(OSError):
            self.status(
                s.status_channel_id,
                f"Aufnahme wartet (Sitzung `{s.id}`): "
                "Alle Anwesenden brauchen einen Alias und müssen "
                "dieser Sitzung ausdrücklich zustimmen. /stop beendet sie vollständig.",
            )
        current = self.store.session(s.id)
        s = current.model_copy(update={"consent_reminder_at": int(time.time())})
        self.store.save_session(current, s)
        return s

    def consent(self, actor: Actor, sid: str, *, cloud: bool) -> Session:
        """Record only optional cloud-processing consent, never recording consent."""
        s = self.store.session(sid)
        self.participant(actor, s)
        if s.state not in (SessionState.WAITING, SessionState.PAUSED):
            raise Conflict("consent requires a paused session")
        s2 = s.model_copy(
            update={
                "cloud_consent": (
                    (s.cloud_consent | {actor.user_id})
                    if cloud
                    else s.cloud_consent - {actor.user_id}
                ),
            }
        )
        self.store.save_session(s, s2)
        return s2

    def alias_consent(self, actor: Actor, sid: str, alias: str, *, accepted: bool) -> Session:
        """Bind the caller's self-chosen alias and recording choice to this session."""
        s = self.store.session(sid)
        self.participant(actor, s)
        if s.state not in (SessionState.WAITING, SessionState.PAUSED, SessionState.RECORDING):
            raise Conflict("session is finalized")
        alias = validate_alias(alias)
        aliases = dict(s.speaker_aliases)
        if any(
            user_id != actor.user_id and value.casefold() == alias.casefold()
            for user_id, value in aliases.items()
        ):
            raise Conflict("alias already used in this voice channel")
        aliases[actor.user_id] = alias
        consented = s.consented | {actor.user_id} if accepted else s.consented - {actor.user_id}
        pause = s.state == SessionState.RECORDING and not accepted
        updated = s.model_copy(
            update={
                "speaker_aliases": aliases,
                "consented": consented,
                "cloud_consent": (
                    s.cloud_consent if accepted else s.cloud_consent - {actor.user_id}
                ),
                "state": SessionState.PAUSED if pause else s.state,
                "stop_reason": "consent_required" if not accepted else s.stop_reason,
                "consent_reminder_at": None if not accepted else s.consent_reminder_at,
            }
        )
        self.store.save_session(s, updated)
        if not accepted:
            if pause:
                self.close_audio(s.id)
            with contextlib.suppress(OSError):
                receipt = self.status(
                    s.status_channel_id or self.config.output_channel_id,
                    "Aufnahme bleibt angehalten: Eine Person hat die Aufzeichnung abgelehnt. "
                    "Neue Zustimmung ist nur über /alias möglich; /stop beendet sie vollständig.",
                )
                self._mark_consent_notice(s.id, receipt)
        return updated

    def remove_session_alias(self, actor: Actor, sid: str) -> Session:
        """Remove the caller's binding and immediately withdraw recording permission."""
        s = self.store.session(sid)
        self.participant(actor, s)
        aliases = dict(s.speaker_aliases)
        aliases.pop(actor.user_id, None)
        recording = s.state == SessionState.RECORDING
        updated = s.model_copy(
            update={
                "speaker_aliases": aliases,
                "consented": s.consented - {actor.user_id},
                "cloud_consent": s.cloud_consent - {actor.user_id},
                "state": SessionState.PAUSED if recording else s.state,
                "stop_reason": "consent_required",
                "consent_reminder_at": None,
            }
        )
        self.store.save_session(s, updated)
        if recording:
            self.close_audio(s.id)
        with contextlib.suppress(OSError):
            receipt = self.status(
                s.status_channel_id or self.config.output_channel_id,
                "Aufnahme pausiert bzw. bleibt pausiert: Alias und Zustimmung fehlen. "
                "Bitte /alias neu ausfüllen oder mit /stop beenden.",
            )
            self._mark_consent_notice(s.id, receipt)
        return updated

    def resume(self, actor: Actor, sid: str) -> Session:
        s = self.owned(actor, sid, "resume")
        if s.state not in (SessionState.WAITING, SessionState.PAUSED):
            raise Conflict("session is not paused")
        if (
            not s.participants
            or not s.participants <= s.speaker_aliases.keys()
            or not s.participants <= s.consented
            or len(s.attendees) > self.config.limits.max_participants
        ):
            raise Denied("every participant needs an alias and recording consent")
        receipt = self.status(
            s.status_channel_id or self.config.output_channel_id,
            "Aufnahme läuft. /stop beendet sofort.",
        )
        if not receipt:
            raise Denied("visible recording status was not acknowledged")
        s2 = s.model_copy(
            update={
                "state": SessionState.RECORDING,
                "stop_reason": None,
                "consent_reminder_at": None,
            }
        )
        self.store.save_session(s, s2)
        return s2

    def pause(self, actor: Actor, sid: str) -> Session:
        s = self.owned(actor, sid, "pause")
        if s.state != SessionState.RECORDING:
            raise Conflict("session is not recording")
        return self._halt(
            s, SessionState.PAUSED, "manual_pause", "Aufnahme pausiert. /resume setzt fort."
        )

    def pause_for_voice_state_loss(self, sid: str) -> Session | None:
        s = self.store.session(sid)
        if s.state != SessionState.RECORDING:
            return None
        return self._halt(
            s,
            SessionState.PAUSED,
            "voice_state_unavailable",
            "Aufnahme pausiert, weil der Voice-Status nicht verfügbar ist. "
            "Sie startet nicht automatisch; bitte /resume prüfen oder /stop verwenden.",
        )

    def stop(self, actor: Actor, sid: str) -> Session:
        s = self.store.session(sid)
        # Every current participant has a local emergency stop, independent of cloud/roles.
        try:
            self.participant(actor, s)
        except Denied:
            self.authorize(actor, "stop")
        if s.state not in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED):
            raise Conflict("session is already finalized")
        stopped = self._halt(
            s, SessionState.STOPPED, "explicit_stop", "Aufnahme vollständig beendet."
        )
        audio = self.store.files.root / "sessions" / sid / "audio"
        if audio.is_dir() and not audio.is_symlink() and next(audio.glob("*.flac"), None):
            self.store.enqueue(sid, "stt")
        return stopped

    def _halt(self, s: Session, state: SessionState, reason: str, message: str) -> Session:
        updated = s.model_copy(
            update={
                "state": state,
                "stop_reason": reason,
                "consent_reminder_at": None,
            }
        )
        self.store.save_session(s, updated)  # Close the write gate before any I/O/network.
        self.close_audio(s.id)
        # Status failure must never undo a local stop.
        receipt = None
        with contextlib.suppress(OSError):
            receipt = self.status(s.status_channel_id or self.config.output_channel_id, message)
        self._mark_consent_notice(s.id, receipt)
        return self.store.session(s.id)

    def _mark_consent_notice(self, sid: str, receipt: str | None) -> None:
        if not receipt:
            return
        current = self.store.session(sid)
        if (
            current.state in (SessionState.WAITING, SessionState.PAUSED)
            and current.stop_reason == "consent_required"
        ):
            self.store.save_session(
                current,
                current.model_copy(update={"consent_reminder_at": int(time.time())}),
            )

    def membership(
        self,
        sid: str,
        participants: frozenset[str],
        *,
        speaker_aliases: dict[str, str] | None = None,
    ) -> Session:
        s = self.store.session(sid)
        if s.state not in (SessionState.WAITING, SessionState.RECORDING, SessionState.PAUSED):
            raise Conflict("session is finalized")
        added = participants - s.participants
        oversized = len(s.attendees | participants) > self.config.limits.max_participants
        aliases = dict(s.speaker_aliases)
        for user_id in added:
            if user_id in (speaker_aliases or {}):
                candidate = validate_alias(speaker_aliases[user_id])
                if not any(
                    other_user != user_id and value.casefold() == candidate.casefold()
                    for other_user, value in aliases.items()
                ):
                    aliases[user_id] = candidate
        needs_pause = bool(added or oversized or not participants)
        reason = (
            "consent_required"
            if added
            else "participant_limit"
            if oversized
            else "no_participants"
            if not participants
            else s.stop_reason
        )
        changed = s.model_copy(
            update={
                "participants": participants,
                "attendees": s.attendees | participants,
                "consented": s.consented & participants,
                "cloud_consent": s.cloud_consent - added,
                "speaker_aliases": aliases,
                "state": SessionState.PAUSED if needs_pause else s.state,
                "stop_reason": reason,
                "consent_reminder_at": None if added else s.consent_reminder_at,
            }
        )
        self.store.save_session(s, changed)
        if needs_pause:
            self.close_audio(s.id)
        if added:
            receipt = None
            with contextlib.suppress(OSError):
                receipt = self.status(
                    s.status_channel_id or self.config.output_channel_id,
                    "Aufnahme pausiert: Eine neue Person ist im Sprachkanal. "
                    "Alle brauchen /alias mit Alias und ausdrücklicher Zustimmung; "
                    "/stop beendet die Aufnahme vollständig. Sie startet nie automatisch.",
                )
            current = self.store.session(s.id)
            reminder_at = int(time.time()) if receipt else None
            if current.consent_reminder_at != reminder_at:
                changed = current.model_copy(update={"consent_reminder_at": reminder_at})
                self.store.save_session(current, changed)
                return changed
        return changed

    def remind_consent(self, sid: str, now: int | None = None) -> bool:
        s = self.store.session(sid)
        if (
            s.state not in (SessionState.WAITING, SessionState.PAUSED)
            or s.stop_reason != "consent_required"
        ):
            return False
        now = int(time.time()) if now is None else now
        if s.consent_reminder_at is not None and now - s.consent_reminder_at < 60:
            return False
        missing_aliases = s.participants - s.speaker_aliases.keys()
        missing_consent = s.participants - s.consented
        if not missing_aliases and not missing_consent:
            return False
        message = (
            "Aufnahme bleibt pausiert: "
            f"{len(missing_aliases)} Alias(e) und {len(missing_consent)} "
            "Aufnahmezustimmung(en) fehlen. Bitte /alias ausfüllen oder mit /stop beenden. "
            "Fortsetzen erfolgt nur nach /resume und vollständiger Zustimmung."
        )
        receipt = None
        with contextlib.suppress(OSError):
            receipt = self.status(s.status_channel_id or self.config.output_channel_id, message)
        if not receipt:
            return False
        current = self.store.session(s.id)
        self.store.save_session(current, current.model_copy(update={"consent_reminder_at": now}))
        return True

    def revoke(self, actor: Actor, sid: str):
        s = self.store.session(sid)
        if (
            actor.guild_id != self.config.guild_id
            or actor.channel_id not in self.config.command_channel_ids
            or actor.user_id not in s.attendees
        ):
            raise Denied("session attendee required")
        message = "Aufnahme pausiert: Zustimmung widerrufen. /alias oder /stop."
        if s.state == SessionState.RECORDING:
            s2 = self._halt(s, SessionState.PAUSED, "consent_required", message)
        elif s.state in (SessionState.WAITING, SessionState.PAUSED):
            s2 = s.model_copy(
                update={
                    "stop_reason": "consent_required",
                    "consent_reminder_at": None,
                }
            )
        else:
            s2 = s
        self.store.save_session(
            s2,
            s2.model_copy(
                update={
                    "consented": s2.consented - {actor.user_id},
                    "cloud_consent": s2.cloud_consent - {actor.user_id},
                }
            ),
        )
        if s.state in (SessionState.WAITING, SessionState.PAUSED):
            receipt = None
            with contextlib.suppress(OSError):
                receipt = self.status(s.status_channel_id or self.config.output_channel_id, message)
            self._mark_consent_notice(s.id, receipt)

    def process(self, actor: Actor, sid: str) -> str:
        self.owned(actor, sid, "process")
        return self.store.enqueue(sid, "stt")

    def restart_analysis(self, actor: Actor, sid: str, settings: InferenceSettings) -> str:
        self.owned(actor, sid, "process")
        if not self.config.cloud_enabled:
            raise Denied("cloud processing disabled")
        return self.store.restart_analysis(sid, settings)

    def result(self, actor: Actor, sid: str) -> dict[str, bytes]:
        self.owned(actor, sid, "result")
        return {
            "protokoll-begruendet.md": self.store.artifact(sid, "result.md"),
            "protokoll-interpretiert.md": self.store.artifact(sid, "summary.md"),
        }

    def transcript(self, actor: Actor, sid: str) -> dict[str, bytes]:
        self.owned(actor, sid, "result")
        if self.store.job_result(sid, "stt") is None:
            raise Conflict("committed transcript required")
        return {"transkript.json": self.store.artifact(sid, "transcript.json")}

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
                            "stop_reason": s.stop_reason,
                            "participants": len(s.participants),
                            "recording_consents": len(s.consented & s.participants),
                            "cloud_consents": len(s.cloud_consent & s.attendees),
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
