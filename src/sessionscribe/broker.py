"""Trusted host-side job orchestration. This module exposes no host command endpoint.

An infrastructure-owned supervisor must implement Runner with fixed units/images and
prove cgroup quiescence. Applications receive only their stage-specific bind mounts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from .analysis import validate_analysis
from .dispatch import Resources, global_stt_lock
from .files import SafeFiles
from .handoff import (
    AnalysisHandoff,
    Binding,
    Checkpoint,
    Completion,
    Phase,
    SttHandoff,
    config_data,
    digest,
    encoded,
    session_data,
)
from .model_cache import REVISION
from .models import AudioChunk, Config, Segment, Session, SessionState, Tenant
from .render import render_analysis
from .store import Conflict, Store
from .transcript import canonicalize, load_chunks

MAX_TRANSCRIPT_BYTES = 16_000_000


class WorkerStillRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class Prepared:
    binding: Binding
    session: Session
    chunks: tuple[AudioChunk, ...]
    input_path: Path
    output_path: Path
    source_session: Session | None = None


class Runner(Protocol):
    def recover(self, phase: Phase) -> bool:
        """Stop/reconcile orphaned fixed workers. STT covers both tenants globally."""
        ...

    def run(self, prepared: Prepared) -> None:
        """Launch only the configured stage unit; wait with a bounded timeout."""
        ...

    def quiesce(self, prepared: Prepared) -> bool:
        """Stop if necessary and prove the worker's entire cgroup has exited."""
        ...


class Workspace:
    def __init__(self, tenant: Tenant, phase: Phase, *, inbox: Path, outbox: Path, cache: Path):
        tenant = TypeAdapter(Tenant).validate_python(tenant, strict=True)
        if phase not in ("stt", "analysis"):
            raise ValueError("invalid broker identity")
        roots = [path.absolute() for path in (inbox, outbox, cache)]
        for i, root in enumerate(roots):
            if any(
                root == other or root in other.parents or other in root.parents
                for other in roots[i + 1 :]
            ):
                raise ValueError("broker input/output/cache roots must be disjoint")
        self.tenant, self.phase = tenant, phase
        self.inbox, self.outbox, self.cache = (SafeFiles(path) for path in roots)
        for role, files in (("input", self.inbox), ("output", self.outbox), ("cache", self.cache)):
            identity = {"tenant": tenant, "phase": phase, "role": role}
            try:
                existing = json.loads(files.read("identity.json", 1024))
            except FileNotFoundError:
                with files.create("identity.json") as stream:
                    stream.write(encoded(identity))
            else:
                if existing != identity:
                    raise ValueError("cross-tenant or cross-stage broker workspace")


class Broker:
    def __init__(
        self,
        store: Store,
        workspace: Workspace,
        config: Config,
        runner: Runner,
        resources: Callable[[], Resources],
        global_lock: Path,
    ):
        if store.tenant != config.tenant or workspace.tenant != store.tenant:
            raise ValueError("cross-tenant broker")
        for files in (workspace.inbox, workspace.outbox, workspace.cache):
            if (
                files.root == store.files.root
                or store.files.root in files.root.parents
                or files.root in store.files.root.parents
            ):
                raise ValueError("recorder and worker stores must be disjoint")
        self.store, self.workspace, self.config = store, workspace, config
        self.runner, self.resources, self.global_lock = runner, resources, global_lock

    def prepare(self, job: dict) -> Prepared:
        self.store.assert_job(job)
        if job["kind"] != self.workspace.phase:
            raise ValueError("wrong broker phase")
        source_session = self.store.session(job["session_id"])
        session = self.store.analysis_session(job) if job["kind"] == "analysis" else source_session
        if session.state not in (SessionState.STOPPED, SessionState.INTERRUPTED):
            raise Conflict("session is not finalized")
        chunks: tuple[AudioChunk, ...] = ()
        if job["kind"] == "stt":
            chunks = tuple(load_chunks(self.store, session.id))
            if len(chunks) > self.config.limits.max_chunks:
                raise ValueError("audio chunk limit")
            # Validate intervals, IDs and paths before copying any audio.
            SttHandoff(schema_version=1, session_id=session.id, chunks=list(chunks))
            if any(
                c.speaker_id not in session.attendees
                or c.end_ms > self.config.limits.max_duration_ms
                for c in chunks
            ):
                raise ValueError("audio outside consented session bounds")
            payload = {
                "session": session_data(session),
                "model_revision": REVISION,
                "chunks": [c.model_dump(mode="json") for c in chunks],
            }
        else:
            if not self.config.cloud_enabled:
                raise PermissionError("cloud processing disabled")
            stt_result = self.store.job_result(session.id, "stt")
            if stt_result is None:
                raise ValueError("committed STT result required")
            transcript = self.store.artifact(session.id, "transcript.json")
            segments = self._segments(transcript, session.id)
            if (
                not segments
                or not session.participants <= session.cloud_consent
                or {s.speaker_id for s in segments} - session.cloud_consent
            ):
                raise PermissionError("cloud processing not consented")
            payload = {
                "session": session_data(session),
                "transcript_sha256": digest(transcript),
                "config_sha256": digest(encoded(config_data(self.config))),
            }
        binding = Binding(
            tenant=self.store.tenant,
            phase=job["kind"],
            job_id=job["id"],
            token=job["token"],
            input_sha256=digest(encoded(payload)),
        )
        # Do not silently change a session/model/transcript between attempts.
        snapshot_name = f"snapshots/{binding.job_id}.json"
        try:
            previous = self.workspace.cache.read(snapshot_name)
        except FileNotFoundError:
            self.workspace.cache.atomic(snapshot_name, encoded(payload))
        else:
            if previous != encoded(payload):
                raise ValueError("job input snapshot changed")
        input_path = self.workspace.inbox.directory(binding.attempt)
        output_path = self.workspace.outbox.directory(binding.attempt)
        inputs, outputs = SafeFiles(input_path), SafeFiles(output_path)
        if binding.phase == "stt":
            total = 0
            for chunk in chunks:
                if (
                    shutil.disk_usage(input_path).free
                    < self.config.limits.min_free_bytes + 6_000_000
                ):
                    raise OSError("snapshot storage reserve")
                total += inputs.copy_verified(
                    self.store.files,
                    f"sessions/{session.id}/{chunk.path}",
                    f"sessions/{session.id}/{chunk.path}",
                    chunk.sha256,
                    limit=6_000_000,
                    prefix=b"fLaC",
                )
                if total > self.config.limits.max_audio_bytes:
                    raise ValueError("snapshot audio quota")
            handoff = SttHandoff(
                schema_version=1, session_id=session.id, chunks=list(chunks), binding=binding
            )
            inputs.json("handoff.json", handoff.model_dump(mode="json"))
            self._seed_checkpoints(binding, chunks, outputs)
        else:
            inputs.atomic("transcript.json", transcript)
            inputs.json("worker-config.json", config_data(self.config))
            handoff = AnalysisHandoff(
                schema_version=1,
                session=session,
                transcript_sha256=digest(transcript),
                binding=binding,
            )
            inputs.json("handoff.json", handoff.model_dump(mode="json"))
        inputs.seal()
        self.store.assert_job(job)
        return Prepared(binding, session, chunks, input_path, output_path, source_session)

    @staticmethod
    def _segments(raw: bytes, sid: str) -> list[Segment]:
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > 50_000:
            raise ValueError("invalid transcript count")
        return canonicalize([Segment.model_validate(value) for value in data], sid)

    def _seed_checkpoints(self, binding: Binding, chunks, outputs: SafeFiles):
        size = 0
        for chunk in chunks:
            name = f"checkpoints/{binding.input_sha256}/{chunk.id}.json"
            try:
                raw = self.workspace.cache.read(name, 1_000_000)
            except FileNotFoundError:
                continue
            Checkpoint.model_validate_json(raw).validate_chunk(chunk)
            size += len(raw)
            if size > MAX_TRANSCRIPT_BYTES:
                raise ValueError("checkpoint quota")
            outputs.atomic(f"chunks/{chunk.id}.json", raw)

    def harvest(self, prepared: Prepared):
        """Call only after quiescence. Preserve verified partial work for bounded retries."""
        if prepared.binding.phase != "stt":
            return
        outputs, size = SafeFiles(prepared.output_path), 0
        for chunk in prepared.chunks:
            try:
                raw = outputs.read(f"chunks/{chunk.id}.json", 1_000_000)
                Checkpoint.model_validate_json(raw).validate_chunk(chunk)
            except (OSError, ValueError):
                continue  # Never preserve an unvalidated or incomplete checkpoint.
            size += len(raw)
            if size > MAX_TRANSCRIPT_BYTES:
                raise ValueError("checkpoint quota")
            self.workspace.cache.atomic(
                f"checkpoints/{prepared.binding.input_sha256}/{chunk.id}.json", raw
            )

    def promote(self, job: dict, prepared: Prepared):
        self.store.assert_job(job)
        if self.store.session(prepared.session.id) != (prepared.source_session or prepared.session):
            raise Conflict("session changed during processing")
        if (
            prepared.binding.phase == "analysis"
            and self.store.analysis_session(job) != prepared.session
        ):
            raise Conflict("analysis plan changed during processing")
        outputs = SafeFiles(prepared.output_path)
        completion = Completion.model_validate_json(outputs.read("complete.json", 8192))
        if completion.binding != prepared.binding or completion.session_id != prepared.session.id:
            raise ValueError("stale or cross-job output binding")
        expected = (
            {"transcript.json"}
            if prepared.binding.phase == "stt"
            else {"analysis.json", "result.md", "summary.md", "inference.json"}
        )
        if set(completion.outputs) != expected:
            raise ValueError("unexpected worker output files")
        data = {name: outputs.read(name, MAX_TRANSCRIPT_BYTES) for name in expected}
        if any(digest(data[name]) != value for name, value in completion.outputs.items()):
            raise ValueError("worker output integrity failure")
        if prepared.binding.phase == "stt":
            if completion.model_revision != REVISION or completion.compute_type != "int8":
                raise ValueError("unexpected STT model or compute type")
            segments = self._segments(data["transcript.json"], prepared.session.id)
            checked = []
            for chunk in prepared.chunks:
                checkpoint = Checkpoint.model_validate_json(
                    outputs.read(f"chunks/{chunk.id}.json", 1_000_000)
                )
                checked.extend(checkpoint.validate_chunk(chunk))
            if segments != canonicalize(checked, prepared.session.id):
                raise ValueError("incomplete or conflicting STT result")
        else:
            inputs = SafeFiles(prepared.input_path)
            segments = self._segments(inputs.read("transcript.json"), prepared.session.id)
            result = validate_analysis(data["analysis.json"].decode(), prepared.session, segments)
            if (
                data["result.md"]
                != render_analysis(result, prepared.session.speaker_aliases, segments).encode()
            ):
                raise ValueError("unexpected rendered model output")
            from .render import render_interpretation

            if (
                data["summary.md"]
                != render_interpretation(
                    result, prepared.session.speaker_aliases, segments
                ).encode()
            ):
                raise ValueError("unexpected rendered interpretation")
            metadata = json.loads(data["inference.json"])
            if metadata.get("settings") != prepared.session.inference.model_dump(mode="json"):
                raise ValueError("changed inference settings")
        # Only immutable token-specific artifacts precede the DB commit. A crash leaves
        # unreferenced files; consumers read only the committed SQLite result pointer.
        for name in expected:
            self.store.files.atomic(f"results/{prepared.binding.attempt}/{name}", data[name])
        followup = None
        if (
            prepared.binding.phase == "stt"
            and self.config.cloud_enabled
            and segments
            and prepared.session.participants <= prepared.session.cloud_consent
            and {s.speaker_id for s in segments} <= prepared.session.cloud_consent
        ):
            followup = "analysis"
        self.store.complete_job(
            job,
            completion.model_dump(mode="json"),
            next_kind=followup,
            protocol_channel_id=(
                prepared.session.protocol_channel_id
                if prepared.binding.phase == "analysis"
                else None
            ),
        )

    def once(self) -> bool:
        phase = self.workspace.phase
        if phase == "analysis" and not self.config.cloud_enabled:
            return False
        # Native stage users/units are shared across guilds. Recovery must never
        # terminate another guild's live evaluator, so each stage is globally serial.
        lock_path = (
            self.global_lock
            if phase == "stt"
            else self.global_lock.with_name(self.global_lock.name + ".analysis")
        )
        lock = global_stt_lock(lock_path)
        with lock as acquired:
            if not acquired or not self.runner.recover(phase) or not self.resources().admits(phase):
                return False
            # Include bounded snapshot, launcher, worker and full-group stop time.
            job = self.store.claim(phase, lease_seconds=4500)
            if job is None:
                return False
            prepared = None
            with self.store.files.lock(f"locks/{job['session_id']}.lock") as session_lock:
                if not session_lock:
                    self.store.finish(job, error="session_busy")
                    return True
                try:
                    prepared = self.prepare(job)
                    try:
                        self.runner.run(prepared)
                    finally:
                        try:
                            stopped = self.runner.quiesce(prepared)
                        except Exception:
                            raise WorkerStillRunning("worker termination is unconfirmed") from None
                        if not stopped:
                            raise WorkerStillRunning("worker termination is unconfirmed")
                    self.harvest(prepared)
                    self.promote(job, prepared)
                except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
                    if prepared is not None:
                        self.harvest(prepared)
                    self.store.finish(job, error="worker_or_handoff_failed")
            return True
