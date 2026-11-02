from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest
from conftest import start

from sessionscribe.broker import Broker, WorkerStillRunning, Workspace
from sessionscribe.dispatch import Resources, global_stt_lock
from sessionscribe.evaluator import run_handoff as evaluate_handoff
from sessionscribe.files import SafeFiles
from sessionscribe.handoff import Checkpoint, Completion, digest
from sessionscribe.model_cache import REVISION
from sessionscribe.models import Segment
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.store import Conflict, Store
from sessionscribe.transcript import canonicalize, load_transcript

GOOD = Resources(8 * 1024**3, 8 * 1024**3, 0, 4)


class SyntheticRunner:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.runs, self.stops = [], []
        self.recovered, self.quiet = True, True
        self.transform = lambda _prepared, _outputs: None
        self.partial_failure = False
        self.seeded = []
        self.cloud_calls = 0

    def recover(self, _phase):
        return self.recovered

    def run(self, prepared):
        self.runs.append(prepared)
        outputs = SafeFiles(prepared.output_path)
        if prepared.binding.phase == "analysis":
            runner = self

            class Cloud:
                def chat(self, request):
                    runner.cloud_calls += 1
                    payload = json.loads(request["messages"][1]["content"])
                    return {
                        "done": True,
                        "done_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "schema_version": 1,
                                    "tenant": runner.config.tenant,
                                    "mode": runner.config.mode,
                                    "session_id": prepared.session.id,
                                    "findings": [
                                        {
                                            "kind": "summary",
                                            "text": "Synthetischer Entwurf.",
                                            "sources": [s["id"] for s in payload["transcript"]],
                                            "owner": None,
                                            "deadline": None,
                                            "character": None,
                                            "category": "unclear"
                                            if runner.config.mode == "dnd"
                                            else None,
                                        }
                                    ],
                                }
                            )
                        },
                    }

            evaluate_handoff(
                prepared.input_path,
                self.state / prepared.binding.job_id,
                prepared.output_path,
                self.config,
                Cloud(),
            )
        else:
            all_segments = []
            self.seeded.append(len(list(prepared.output_path.glob("chunks/*.json"))))
            for chunk in prepared.chunks:
                segment = Segment(
                    id=chunk.id + ":0000",
                    session_id=chunk.session_id,
                    speaker_id=chunk.speaker_id,
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    text="Synthetische Probe: kein Beschluss.",
                    gap_before_ms=chunk.gap_before_ms,
                )
                outputs.json(
                    f"chunks/{chunk.id}.json",
                    Checkpoint(audio_sha256=chunk.sha256, segments=[segment]).model_dump(
                        mode="json"
                    ),
                )
                all_segments.append(segment)
                if self.partial_failure:
                    raise subprocess.TimeoutExpired("synthetic-worker", 1)
            outputs.json(
                "transcript.json",
                [
                    s.model_dump(mode="json")
                    for s in canonicalize(all_segments, prepared.session.id)
                ],
            )
            completion = Completion(
                schema_version=1,
                session_id=prepared.session.id,
                binding=prepared.binding,
                model_revision=REVISION,
                compute_type="int8",
                outputs={"transcript.json": digest(outputs.read("transcript.json"))},
            )
            outputs.json("complete.json", completion.model_dump(mode="json"))
        self.transform(prepared, outputs)

    def quiesce(self, prepared):
        self.stops.append(prepared)
        return self.quiet


def queued(rig):
    start(rig)
    for t in range(0, 80, 20):
        rig.recorder.accept(Frame(rig.actor.user_id, t, bytes(FRAME_BYTES)))
        if t >= 40:
            rig.recorder.accept(Frame(rig.other.user_id, t, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    rig.control.process(rig.actor, rig.session.id)


def broker(rig, tmp_path, phase="stt", runner=None, config=None):
    config = config or rig.config
    runner = runner or SyntheticRunner(config, tmp_path / "evaluator-state")
    workspace = Workspace(
        config.tenant,
        phase,
        inbox=tmp_path / f"{phase}-inputs",
        outbox=tmp_path / f"{phase}-outputs",
        cache=tmp_path / f"{phase}-cache",
    )
    return Broker(rig.store, workspace, config, runner, lambda: GOOD, tmp_path / "global.lock")


def test_synthetic_recording_to_both_broker_stages_and_validated_result(rig, tmp_path):
    queued(rig)
    stt = broker(rig, tmp_path)
    assert stt.once()
    assert [j["state"] for j in rig.store.queue_status()] == ["succeeded", "pending"]
    prepared = stt.runner.runs[0]
    assert prepared.input_path.stat().st_mode & 0o777 == 0o500
    assert (prepared.input_path / "handoff.json").stat().st_mode & 0o777 == 0o400
    assert not (prepared.input_path / "state.sqlite3").exists()
    assert not (prepared.input_path / "config").exists()
    assert len(load_transcript(rig.store, rig.session.id)) == 2
    evaluation = broker(rig, tmp_path, "analysis")
    assert evaluation.once()
    assert all(j["state"] == "succeeded" for j in rig.store.queue_status())
    assert evaluation.runner.cloud_calls == 1
    assert rig.control.result(rig.actor, rig.session.id)
    assert rig.store.job_result(rig.session.id, "analysis")["binding"]["tenant"] == "1000"
    assert not stt.once() and not evaluation.once()
    assert not list(evaluation.runner.runs[0].input_path.rglob("*.flac"))


def test_evaluator_recovery_is_global_per_stage_and_independent_of_stt(rig, tmp_path):
    queued(rig)
    assert broker(rig, tmp_path).once()
    evaluation = broker(rig, tmp_path, "analysis")
    with global_stt_lock(tmp_path / "global.lock.analysis") as held:
        assert held and not evaluation.once()
        assert not evaluation.runner.runs
        assert rig.store.queue_status()[1]["state"] == "pending"
    with global_stt_lock(tmp_path / "global.lock") as held:
        assert held and evaluation.once()
    assert evaluation.runner.cloud_calls == 1


@pytest.mark.parametrize(
    "field,value",
    [("token", "b" * 32), ("job_id", "c" * 32), ("tenant", "2000"), ("input_sha256", "e" * 64)],
)
def test_stale_or_cross_tenant_completion_is_not_committed(rig, tmp_path, field, value):
    queued(rig)
    b = broker(rig, tmp_path)

    def mutate(_prepared, output):
        data = json.loads(output.read("complete.json"))
        data["binding"][field] = value
        output.json("complete.json", data)

    b.runner.transform = mutate
    assert b.once()
    assert rig.store.queue_status()[0]["state"] == "pending"
    assert rig.store.job_result(rig.session.id, "stt") is None
    with pytest.raises(FileNotFoundError):
        load_transcript(rig.store, rig.session.id)


@pytest.mark.parametrize("damage", ["speaker", "time", "missing_checkpoint", "hash", "model"])
def test_untrusted_stt_output_is_checked_before_promotion(rig, tmp_path, damage):
    queued(rig)
    b = broker(rig, tmp_path)

    def mutate(prepared, output):
        if damage == "missing_checkpoint":
            output.remove(f"chunks/{prepared.chunks[0].id}.json")
        elif damage == "model":
            data = json.loads(output.read("complete.json"))
            data["model_revision"] = "unknown"
            output.json("complete.json", data)
        else:
            data = json.loads(output.read("transcript.json"))
            data[0]["speaker_id" if damage == "speaker" else "end_ms"] = (
                "9999" if damage == "speaker" else 999999
            )
            output.json("transcript.json", data)
            if damage != "hash":
                done = json.loads(output.read("complete.json"))
                done["outputs"]["transcript.json"] = digest(output.read("transcript.json"))
                output.json("complete.json", done)

    b.runner.transform = mutate
    assert b.once()
    assert rig.store.job_result(rig.session.id, "stt") is None
    assert rig.store.queue_status()[0]["state"] == "pending"


def test_lease_expiry_and_consent_changes_fence_worker_outputs(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    b.runner.transform = lambda _p, _o: rig.store.db.execute("UPDATE jobs SET lease_until=0")
    with pytest.raises(Conflict):
        b.once()
    assert rig.store.job_result(rig.session.id, "stt") is None
    assert b.runner.stops


def test_partial_timeout_preserves_and_reuses_only_validated_checkpoints(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    b.runner.partial_failure = True
    assert b.once()
    assert b.runner.stops and rig.store.queue_status()[0]["state"] == "pending"
    assert len(list(b.workspace.cache.root.glob("checkpoints/*/*.json"))) == 1
    rig.store.db.execute("UPDATE jobs SET available=0")
    b.runner.partial_failure = False
    assert b.once()
    assert b.runner.seeded == [0, 1]
    assert b.runner.runs[0].binding.token != b.runner.runs[1].binding.token
    assert rig.store.job_result(rig.session.id, "stt")


@pytest.mark.parametrize("failure", ["busy", "exception"])
def test_unconfirmed_worker_exit_blocks_retries(rig, tmp_path, failure):
    queued(rig)
    b = broker(rig, tmp_path)
    if failure == "exception":

        def unknown(_prepared):
            raise OSError("synthetic supervisor failure")

        b.runner.quiesce = unknown
    else:
        b.runner.quiet = False
    with pytest.raises(WorkerStillRunning):
        b.once()
    assert rig.store.queue_status()[0]["state"] == "running"
    assert rig.store.job_result(rig.session.id, "stt") is None
    b.runner.recovered = False
    assert not b.once()
    assert len(b.runner.runs) == 1


def test_recovery_admission_and_global_lock_precede_claim(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    b.runner.recovered = False
    assert not b.once()
    b.runner.recovered = True
    b.resources = lambda: Resources(1, 1, 99, 1)
    assert not b.once()
    b.resources = lambda: GOOD
    with global_stt_lock(b.global_lock) as held:
        assert held and not b.once()
    assert rig.store.queue_status()[0]["attempts"] == 0


def test_cloud_disabled_or_unconsented_does_not_launch_evaluator(rig, tmp_path):
    queued(rig)
    disabled = rig.config.model_copy(update={"cloud_enabled": False})
    stt = broker(rig, tmp_path, config=disabled)
    assert stt.once()
    assert len(rig.store.queue_status()) == 1
    rig.store.enqueue(rig.session.id, "analysis")
    evaluation = broker(rig, tmp_path, "analysis", config=disabled)
    assert not evaluation.once() and not evaluation.runner.runs
    assert rig.store.queue_status()[1]["attempts"] == 0


def test_cross_tenant_and_overlapping_workspace_rejected(rig, tmp_path):
    b = broker(rig, tmp_path)
    with pytest.raises(ValueError, match="cross-tenant"):
        Workspace(
            "2000",
            "stt",
            inbox=b.workspace.inbox.root,
            outbox=tmp_path / "other-output",
            cache=tmp_path / "other-cache",
        )
    with pytest.raises(ValueError, match="disjoint"):
        Workspace(
            "1000",
            "stt",
            inbox=tmp_path / "input",
            outbox=tmp_path / "input/sub",
            cache=tmp_path / "cache",
        )


def test_snapshot_rejects_symlinks_and_changed_audio_without_losing_original(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    audio = next((rig.store.files.root / "sessions").rglob("*.flac"))
    original = audio.read_bytes()
    target = tmp_path / "outside.flac"
    target.write_bytes(original)
    audio.unlink()
    audio.symlink_to(target)
    assert b.once()
    assert not b.runner.runs
    assert target.read_bytes() == original
    assert rig.store.queue_status()[0]["state"] == "pending"


def test_committed_artifact_hash_is_verified_on_read(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    assert b.once()
    result = rig.store.job_result(rig.session.id, "stt")
    attempt = result["binding"]["job_id"] + "-" + result["binding"]["token"]
    rig.store.files.atomic(f"results/{attempt}/transcript.json", b"[]")
    with pytest.raises(ValueError, match="integrity"):
        load_transcript(rig.store, rig.session.id)


def test_schema_one_migrates_with_jobs_preserved_and_result_fks_cascade(rig, tmp_path):
    queued(rig)
    backup = tmp_path / "restore/state.sqlite3"
    backup.parent.mkdir()
    rig.store.backup(backup)
    with sqlite3.connect(backup) as db:
        db.execute("DROP TABLE job_results")
        db.execute("PRAGMA user_version=1")
    restored = Store(backup.parent, "1000")
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert restored.migration_backup is not None
        with sqlite3.connect(restored.migration_backup) as before:
            assert before.execute("PRAGMA user_version").fetchone()[0] == 1
        assert restored.queue_status()[0]["state"] == "pending"
        assert restored.session(rig.session.id).id == rig.session.id
    finally:
        restored.close()


def test_deletion_removes_committed_recorder_artifacts_and_result_rows(rig, tmp_path):
    queued(rig)
    assert broker(rig, tmp_path).once()
    result = rig.store.job_result(rig.session.id, "stt")
    attempt = result["binding"]["job_id"] + "-" + result["binding"]["token"]
    directory = rig.store.files.root / "results" / attempt
    assert directory.exists()
    rig.control.delete(rig.actor, rig.session.id)
    assert not directory.exists()
    assert rig.store.db.execute("SELECT COUNT(*) FROM job_results").fetchone()[0] == 0
    assert rig.control.queue(rig.actor) == []


def test_retry_refuses_changed_session_snapshot(rig, tmp_path):
    queued(rig)
    b = broker(rig, tmp_path)
    b.runner.partial_failure = True
    assert b.once()
    session = rig.store.session(rig.session.id)
    rig.store.save_session(
        session, session.model_copy(update={"campaign": "changed synthetic input"})
    )
    rig.store.db.execute("UPDATE jobs SET available=0")
    b.runner.partial_failure = False
    assert b.once()
    assert len(b.runner.runs) == 1
    assert rig.store.job_result(rig.session.id, "stt") is None


def test_disk_error_before_result_commit_leaves_no_visible_success(rig, tmp_path, monkeypatch):
    queued(rig)
    b = broker(rig, tmp_path)
    atomic = rig.store.files.atomic

    def full(relative, data):
        if relative.startswith("results/"):
            raise OSError("synthetic ENOSPC")
        return atomic(relative, data)

    monkeypatch.setattr(rig.store.files, "atomic", full)
    assert b.once()
    assert rig.store.job_result(rig.session.id, "stt") is None
    assert rig.store.queue_status()[0]["state"] == "pending"
    assert list((rig.store.files.root / "sessions").rglob("*.flac"))


def test_deletion_removes_orphaned_uncommitted_attempt_files(rig, tmp_path):
    queued(rig)
    job = rig.store.claim("stt")
    orphan = f"results/{job['id']}-{'f' * 32}"
    rig.store.files.atomic(f"{orphan}/transcript.json", b"synthetic partial output")
    rig.store.finish(job, error="synthetic_lost_commit")
    rig.control.delete(rig.actor, rig.session.id)
    assert not (rig.store.files.root / orphan).exists()


def test_result_and_followup_commit_roll_back_together(rig, tmp_path, monkeypatch):
    queued(rig)
    b = broker(rig, tmp_path)

    def failed_enqueue(*_args, **_kwargs):
        raise OSError("synthetic DB failure while enqueuing analysis")

    monkeypatch.setattr(rig.store, "enqueue", failed_enqueue)
    assert b.once()
    assert rig.store.job_result(rig.session.id, "stt") is None
    assert len(rig.store.queue_status()) == 1
    assert rig.store.queue_status()[0]["state"] == "pending"


def test_result_ledger_refuses_a_forged_job_identity(rig, tmp_path):
    queued(rig)
    assert broker(rig, tmp_path).once()
    result = rig.store.job_result(rig.session.id, "stt")
    result["binding"]["job_id"] = "e" * 32
    rig.store.db.execute("UPDATE job_results SET manifest=?", (json.dumps(result),))
    with pytest.raises(ValueError, match="mismatched"):
        load_transcript(rig.store, rig.session.id)


def failed_analysis(rig, tmp_path):
    queued(rig)
    assert broker(rig, tmp_path).once()
    for index in range(3):
        now = 10_000_000_000.0 + index * 1000
        job = rig.store.claim("analysis", now=now)
        assert job is not None
        rig.store.finish(job, error="synthetic_incomplete_response", now=now + 1)
    return next(j for j in rig.store.queue_status() if j["kind"] == "analysis")


def test_new_analysis_job_preserves_recording_failed_history_and_frozen_plan(rig, tmp_path):
    failed = failed_analysis(rig, tmp_path)
    source = rig.store.session(rig.session.id)
    original = source.model_dump_json()
    settings = source.inference.model_copy(
        update={"thinking": "low", "context_tokens": 16384, "output_tokens": 4096}
    )
    with pytest.raises(PermissionError):
        rig.control.restart_analysis(rig.other, source.id, settings)
    with pytest.raises(ValueError, match="model"):
        rig.control.restart_analysis(
            rig.actor, source.id, settings.model_copy(update={"model": "different-fixture"})
        )
    assert rig.store.db.execute("SELECT count(*) FROM retired_jobs").fetchone()[0] == 0
    new_id = rig.control.restart_analysis(rig.actor, source.id, settings)
    assert new_id != failed["id"]
    assert rig.store.session(source.id).model_dump_json() == original
    retired = json.loads(rig.store.db.execute("SELECT body FROM retired_jobs").fetchone()[0])
    assert retired["job"]["id"] == failed["id"] and retired["job"]["attempts"] == 3
    assert retired["job"]["state"] == "dead"
    new_job = next(j for j in rig.store.queue_status() if j["kind"] == "analysis")
    assert new_job["id"] == new_id and new_job["attempts"] == 0
    evaluation = broker(rig, tmp_path, "analysis")
    assert evaluation.once()
    assert evaluation.runner.runs[0].session.inference == settings
    assert evaluation.runner.runs[0].source_session == source
    metadata = json.loads(rig.store.artifact(source.id, "inference.json"))
    assert metadata["settings"]["thinking"] == "low"
    assert rig.store.job_result(source.id, "analysis")["binding"]["job_id"] == new_id
    assert rig.store.session(source.id).model_dump_json() == original
    with pytest.raises(Conflict):
        rig.control.restart_analysis(rig.actor, source.id, settings)


def test_analysis_plan_source_change_and_retired_result_cleanup(rig, tmp_path):
    failed = failed_analysis(rig, tmp_path)
    source = rig.store.session(rig.session.id)
    settings = source.inference.model_copy(update={"thinking": "low"})
    rig.control.restart_analysis(rig.actor, source.id, settings)
    rig.store.save_session(source, source.model_copy(update={"cloud_consent": frozenset()}))
    evaluation = broker(rig, tmp_path, "analysis")
    assert evaluation.once()
    assert not evaluation.runner.runs
    assert rig.store.job_result(source.id, "analysis") is None
    old_result = f"results/{failed['id']}-{'a' * 32}"
    rig.store.files.atomic(old_result + "/uncommitted.json", b"{}")
    rig.control.delete(rig.actor, source.id)
    assert not (rig.store.files.root / old_result).exists()
    for table in ("analysis_plans", "retired_jobs"):
        assert rig.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_analysis_restart_transaction_cannot_lose_failed_job(rig, tmp_path):
    failed = failed_analysis(rig, tmp_path)
    source = rig.store.session(rig.session.id)
    rig.store.db.execute(
        "CREATE TRIGGER reject_new_plan BEFORE INSERT ON analysis_plans "
        "BEGIN SELECT RAISE(ABORT, 'synthetic plan storage failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        rig.control.restart_analysis(
            rig.actor, source.id, source.inference.model_copy(update={"thinking": "low"})
        )
    current = next(j for j in rig.store.queue_status() if j["kind"] == "analysis")
    assert current == failed
    assert rig.store.db.execute("SELECT count(*) FROM retired_jobs").fetchone()[0] == 0
    assert rig.store.db.execute("SELECT count(*) FROM analysis_plans").fetchone()[0] == 0


def test_inflight_analysis_plan_change_cannot_commit(rig, tmp_path):
    failed_analysis(rig, tmp_path)
    source = rig.store.session(rig.session.id)
    low = source.inference.model_copy(update={"thinking": "low"})
    new_id = rig.control.restart_analysis(rig.actor, source.id, low)
    evaluation = broker(rig, tmp_path, "analysis")

    def change_plan(_prepared, _outputs):
        rig.store.db.execute(
            "UPDATE analysis_plans SET inference=? WHERE job_id=?",
            (low.model_copy(update={"thinking": "high"}).model_dump_json(), new_id),
        )

    evaluation.runner.transform = change_plan
    with pytest.raises(Conflict, match="plan changed"):
        evaluation.once()
    assert rig.store.job_result(source.id, "analysis") is None
    assert not rig.store.protocol_queue()
    assert rig.store.session(source.id) == source
