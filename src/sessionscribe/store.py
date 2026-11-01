from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from .files import SafeFiles
from .models import Session, SessionState, Tenant


class Conflict(RuntimeError):
    pass


class Store:
    """One private database per tenant. No caller-supplied database paths in jobs."""

    def __init__(self, root: Path, tenant: Tenant):
        tenant = TypeAdapter(Tenant).validate_python(tenant, strict=True)
        self.files = SafeFiles(root)
        self.tenant = tenant
        db = self.files.root / "state.sqlite3"
        if db.is_symlink():
            raise ValueError("symlink database")
        self.db = sqlite3.connect(db, isolation_level=None, timeout=5)
        os.chmod(db, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self.db.close()
            raise ValueError("unsupported database schema")
        try:
            with self.transaction():
                schema = """
                    CREATE TABLE IF NOT EXISTS identity (tenant TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY, body TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                        kind TEXT NOT NULL CHECK(kind IN ('stt','analysis')),
                        state TEXT NOT NULL DEFAULT 'pending'
                          CHECK(state IN ('pending','running','succeeded','dead')),
                        attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
                        lease_until REAL, token TEXT, error TEXT,
                        UNIQUE(session_id,kind));
                    CREATE TABLE IF NOT EXISTS budgets (
                        day TEXT PRIMARY KEY, calls INTEGER NOT NULL, tokens INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS deliveries (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                        state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain')),
                        receipt TEXT);
                    CREATE UNIQUE INDEX IF NOT EXISTS active_voice_session
                      ON sessions(json_extract(body,'$.voice_channel_id'))
                      WHERE json_extract(body,'$.state')
                        IN ('waiting_consent','recording','paused');
                    CREATE UNIQUE INDEX IF NOT EXISTS active_recording_bot
                      ON sessions(COALESCE(json_extract(body,'$.recorder_bot_id'),
                        'single-control-bot'))
                      WHERE json_extract(body,'$.state')
                        IN ('waiting_consent','recording','paused');
                    CREATE TABLE IF NOT EXISTS protocol_outbox (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                        channel_id TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending'
                          CHECK(state IN ('pending','sending','sent','uncertain','blocked')),
                        error TEXT);
                    CREATE TABLE IF NOT EXISTS job_results (
                        job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                        token TEXT NOT NULL, manifest TEXT NOT NULL);
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                row = self.db.execute("SELECT tenant FROM identity").fetchone()
                if row and row[0] != tenant:
                    raise ValueError("cross-tenant database")
                self.db.execute("INSERT OR IGNORE INTO identity VALUES (?)", (tenant,))
                for saved in self.db.execute("SELECT body FROM sessions"):
                    if Session.model_validate_json(saved[0]).tenant != tenant:
                        raise ValueError("legacy store requires explicit guild migration")
                self.db.execute("PRAGMA user_version=4")
        except BaseException:
            self.db.close()
            raise

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        else:
            if self.db.in_transaction:
                self.db.commit()

    def add_session(self, session: Session):
        if session.tenant != self.tenant:
            raise ValueError("cross-tenant session")
        try:
            self.db.execute(
                "INSERT INTO sessions(id,body) VALUES (?,?)",
                (session.id, session.model_dump_json()),
            )
        except sqlite3.IntegrityError:
            raise Conflict("voice channel or recording bot already has an active session") from None

    def session(self, sid: str) -> Session:
        row = self.db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError("unknown session")
        session = Session.model_validate_json(row[0])
        if session.tenant != self.tenant or session.state == SessionState.DELETED:
            raise KeyError("unavailable session")
        return session

    def save_session(self, expected: Session, updated: Session):
        if updated.id != expected.id or updated.tenant != self.tenant:
            raise ValueError("session identity cannot change")
        Session.model_validate(updated.model_dump())
        row = self.db.execute(
            "SELECT body,revision FROM sessions WHERE id=?", (expected.id,)
        ).fetchone()
        if row is None or Session.model_validate_json(row["body"]) != expected:
            raise Conflict("session changed")
        result = self.db.execute(
            "UPDATE sessions SET body=?,revision=revision+1 WHERE id=? AND revision=?",
            (updated.model_dump_json(), expected.id, row["revision"]),
        )
        if result.rowcount != 1:
            raise Conflict("session changed")

    def interrupt_recordings(self) -> int:
        count = 0
        for row in self.db.execute("SELECT id FROM sessions").fetchall():
            with contextlib.suppress(KeyError):
                s = self.session(row[0])
                if s.state in (SessionState.RECORDING, SessionState.PAUSED, SessionState.WAITING):
                    self.save_session(
                        s,
                        s.model_copy(
                            update={
                                "state": SessionState.INTERRUPTED,
                                "stop_reason": "process_restart",
                            }
                        ),
                    )
                    count += 1
        return count

    def enqueue(self, sid: str, kind: Literal["stt", "analysis"]) -> str:
        s = self.session(sid)
        if s.state not in (SessionState.STOPPED, SessionState.INTERRUPTED):
            raise Conflict("session is not finalized")
        jid = uuid.uuid4().hex
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,session_id,kind) VALUES (?,?,?)", (jid, sid, kind)
        )
        return self.db.execute(
            "SELECT id FROM jobs WHERE session_id=? AND kind=?", (sid, kind)
        ).fetchone()[0]

    def claim(
        self,
        kind: str,
        *,
        now: float | None = None,
        lease_seconds: int = 1800,
        max_attempts: int = 3,
    ) -> dict | None:
        now = time.time() if now is None else now
        with self.transaction():
            self.db.execute(
                """UPDATE jobs SET state=CASE WHEN attempts>=? THEN 'dead'
                ELSE 'pending' END, token=NULL, lease_until=NULL, error='lease_expired', available=?
                WHERE kind=? AND state='running' AND lease_until<=?""",
                (max_attempts, now + 30, kind, now),
            )
            row = self.db.execute(
                """SELECT * FROM jobs WHERE kind=? AND state='pending'
                AND available<=? AND attempts<? ORDER BY rowid LIMIT 1""",
                (kind, now, max_attempts),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute(
                """UPDATE jobs SET state='running',attempts=attempts+1,
                lease_until=?,token=? WHERE id=?""",
                (now + lease_seconds, token, row["id"]),
            )
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def finish(self, job: dict, *, error: str | None = None, now: float | None = None):
        now = time.time() if now is None else now
        state = "succeeded" if error is None else ("dead" if job["attempts"] >= 3 else "pending")
        delay = min(3600, 30 * 2 ** job["attempts"])
        result = self.db.execute(
            """UPDATE jobs SET state=?,error=?,available=?,token=NULL,
            lease_until=NULL WHERE id=? AND token=? AND state='running' AND lease_until>?""",
            (state, error, now + delay, job["id"], job["token"], now),
        )
        if result.rowcount != 1:
            raise Conflict("stale lease")

    def assert_job(self, job: dict, *, now: float | None = None):
        now = time.time() if now is None else now
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if (
            row is None
            or row["state"] != "running"
            or row["token"] != job["token"]
            or row["session_id"] != job["session_id"]
            or row["kind"] != job["kind"]
            or row["attempts"] != job["attempts"]
            or row["lease_until"] <= now
        ):
            raise Conflict("stale or mismatched job lease")
        self.session(row["session_id"])

    def complete_job(
        self,
        job: dict,
        manifest: dict,
        *,
        now: float | None = None,
        next_kind: Literal["analysis"] | None = None,
        protocol_channel_id: str | None = None,
    ):
        from .handoff import Completion

        completion = Completion.model_validate(manifest)
        binding = completion.binding
        if (
            binding is None
            or binding.job_id != job["id"]
            or binding.token != job["token"]
            or binding.phase != job["kind"]
            or binding.tenant != self.tenant
            or completion.session_id != job["session_id"]
        ):
            raise ValueError("mismatched committed job result")
        with self.transaction():
            self.assert_job(job, now=now)
            self.db.execute(
                "INSERT INTO job_results VALUES (?,?,?)",
                (job["id"], job["token"], json.dumps(manifest, sort_keys=True)),
            )
            self.finish(job, now=now)
            if next_kind is not None:
                self.enqueue(job["session_id"], next_kind)
            if protocol_channel_id is not None:
                session = self.session(job["session_id"])
                if job["kind"] != "analysis" or session.protocol_channel_id != protocol_channel_id:
                    raise ValueError("unexpected protocol destination")
                self.db.execute(
                    "INSERT OR IGNORE INTO protocol_outbox(session_id,channel_id) VALUES (?,?)",
                    (session.id, protocol_channel_id),
                )

    def job_result(self, sid: str, kind: str) -> dict | None:
        self.session(sid)
        row = self.db.execute(
            """SELECT r.manifest,r.job_id,r.token FROM job_results r JOIN jobs j ON j.id=r.job_id
               WHERE j.session_id=? AND j.kind=? AND j.state='succeeded'""",
            (sid, kind),
        ).fetchone()
        if row is None:
            return None
        from .handoff import Completion

        completion = Completion.model_validate_json(row["manifest"])
        binding = completion.binding
        if (
            binding is None
            or binding.job_id != row["job_id"]
            or binding.token != row["token"]
            or binding.tenant != self.tenant
            or binding.phase != kind
            or completion.session_id != sid
        ):
            raise ValueError("mismatched committed job result")
        return completion.model_dump(mode="json")

    def artifact(self, sid: str, name: str) -> bytes:
        kind = "stt" if name == "transcript.json" else "analysis"
        if name not in ("transcript.json", "analysis.json", "result.md", "inference.json"):
            raise ValueError("unknown artifact")
        result = self.job_result(sid, kind)
        if result is None:
            return self.files.read(f"sessions/{sid}/{name}")  # Standalone local workflows.
        from .handoff import Completion

        completion = Completion.model_validate(result)
        binding = completion.binding
        if (
            binding is None
            or binding.phase != kind
            or binding.tenant != self.tenant
            or completion.session_id != sid
            or name not in completion.outputs
            or not re.fullmatch(r"[a-f0-9]{32}-[a-f0-9]{32}", binding.attempt)
        ):
            raise ValueError("invalid committed artifact manifest")
        data = self.files.read(f"results/{binding.attempt}/{name}")
        if hashlib.sha256(data).hexdigest() != completion.outputs[name]:
            raise ValueError("committed artifact integrity failure")
        return data

    def reserve_budget(self, day: str, tokens: int, *, max_calls: int, max_tokens: int):
        if tokens < 1:
            raise ValueError("invalid reservation")
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO budgets VALUES (?,0,0)", (day,))
            result = self.db.execute(
                """UPDATE budgets SET calls=calls+1,tokens=tokens+?
                WHERE day=? AND calls+1<=? AND tokens+?<=?""",
                (tokens, day, max_calls, tokens, max_tokens),
            )
            if result.rowcount != 1:
                raise Conflict("inference budget exhausted")

    def begin_delivery(self, sid: str) -> bool:
        self.session(sid)
        with self.transaction():
            result = self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,'sending',NULL)", (sid,)
            )
            return result.rowcount == 1

    def end_delivery(self, sid: str, receipt: str | None):
        self.db.execute(
            "UPDATE deliveries SET state=?,receipt=? WHERE session_id=?",
            ("sent" if receipt else "uncertain", receipt, sid),
        )

    def protocol_queue(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT session_id,channel_id,state,error FROM protocol_outbox ORDER BY rowid"
            )
        ]

    def protocol_state(self, sid: str, state: str, error: str | None = None):
        if state not in ("pending", "sending", "sent", "uncertain", "blocked"):
            raise ValueError("invalid protocol outbox state")
        self.db.execute(
            "UPDATE protocol_outbox SET state=?,error=? WHERE session_id=?", (state, error, sid)
        )

    def queue_status(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,session_id,kind,state,attempts,error FROM jobs ORDER BY rowid"
            )
        ]

    def close(self):
        self.db.close()

    def backup(self, target: Path):
        if target.exists() or target.is_symlink():
            raise ValueError("backup target exists")
        with contextlib.closing(sqlite3.connect(target)) as backup:
            self.db.backup(backup)
        os.chmod(target, 0o600)
