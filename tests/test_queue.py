from pathlib import Path

import pytest

from sessionscribe.dispatch import Dispatcher, Resources, global_stt_lock, worker_argv
from sessionscribe.models import SessionState
from sessionscribe.store import Conflict, Store

GOOD = Resources(8 * 1024**3, 4 * 1024**3, 0, 4)


def enqueue(rig):
    rig.control.stop(rig.actor, rig.session.id)
    return rig.control.process(rig.actor, rig.session.id)


def test_enqueue_and_completion_are_idempotent_and_durable(rig):
    jid = enqueue(rig)
    assert rig.control.process(rig.actor, rig.session.id) == jid
    second = Store(rig.store.files.root, "1000")
    try:
        job = second.claim("stt", now=100)
        assert job["id"] == jid
        assert rig.store.claim("stt", now=100) is None
        second.finish(job, now=101)
        assert rig.store.queue_status()[0]["state"] == "succeeded"
        with pytest.raises(Conflict):
            second.finish(job, now=102)
    finally:
        second.close()


def test_lease_expiry_backoff_and_bounded_retries(rig):
    enqueue(rig)
    first = rig.store.claim("stt", now=100, lease_seconds=10)
    assert rig.store.claim("stt", now=111) is None
    with pytest.raises(Conflict):
        rig.store.finish(first, now=112)
    second = rig.store.claim("stt", now=142, lease_seconds=10)
    rig.store.finish(second, error="synthetic_timeout", now=143)
    assert rig.store.claim("stt", now=144) is None
    third = rig.store.claim("stt", now=300)
    rig.store.finish(third, error="synthetic_timeout", now=301)
    assert rig.store.queue_status()[0]["state"] == "dead"
    assert rig.store.claim("stt", now=10000) is None


@pytest.mark.parametrize(
    "resources",
    [
        Resources(1024, 4 * 1024**3, 0, 4),
        Resources(8 * 1024**3, 1024, 0, 4),
        Resources(8 * 1024**3, 4 * 1024**3, 99, 4),
    ],
)
def test_admission_does_not_consume_attempts(rig, tmp_path, resources):
    enqueue(rig)
    dispatcher = Dispatcher(
        rig.store, tmp_path / "global.lock", lambda _: pytest.fail("launched"), lambda: resources
    )
    assert not dispatcher.once()
    assert rig.store.queue_status()[0]["attempts"] == 0


def test_same_global_lock_excludes_tenants(rig, tmp_path):
    enqueue(rig)
    lock = tmp_path / "global.lock"
    calls = []
    dispatcher = Dispatcher(rig.store, lock, calls.append, lambda: GOOD)
    with global_stt_lock(lock) as held:
        assert held
        assert not dispatcher.once()
    assert dispatcher.once()
    assert len(calls) == 1
    assert rig.store.queue_status()[0]["state"] == "succeeded"


def test_backup_restores_database_and_tenant_identity(rig, tmp_path):
    enqueue(rig)
    backup = tmp_path / "restore" / "state.sqlite3"
    backup.parent.mkdir(mode=0o700)
    rig.store.backup(backup)
    restored = Store(backup.parent, "1000")
    assert restored.session(rig.session.id).state == SessionState.STOPPED
    assert len(restored.queue_status()) == 1
    restored.close()
    with pytest.raises(ValueError, match="cross-tenant"):
        Store(backup.parent, "2000")


def test_fixed_worker_has_no_network_or_credentials(tmp_path):
    args = worker_argv(
        image="registry.example.invalid/sessionscribe/stt@sha256:" + "a" * 64,
        input_path=tmp_path / "input",
        output_path=tmp_path / "output",
        model_path=tmp_path / "model",
        seccomp_path=tmp_path / "seccomp.json",
    )
    for value in ("--network=none", "--read-only", "--cap-drop=all", "--memory=2560m", "--cpus=2"):
        assert value in args
    assert not any("socket" in a or "TOKEN" in a or "--privileged" in a for a in args)
    with pytest.raises(ValueError):
        worker_argv(
            image="latest",
            input_path=Path("/input"),
            output_path=Path("/output"),
            model_path=Path("/model"),
            seccomp_path=Path("/seccomp.json"),
        )


def test_session_revision_fences_stale_writers_independent_of_set_order(rig):
    s = rig.store.session(rig.session.id)
    first = s.model_copy(update={"participants": frozenset({"1001", "1005", "1006"})})
    rig.store.save_session(s, first)
    fetched = rig.store.session(s.id)
    rig.store.save_session(fetched, fetched.model_copy(update={"stop_reason": "synthetic"}))
    with pytest.raises(Conflict):
        rig.store.save_session(fetched, first)


def test_global_stt_lock_excludes_a_separate_process(tmp_path):
    import subprocess
    import sys

    lock = tmp_path / "global.lock"
    script = (
        "from pathlib import Path; from sessionscribe.dispatch import global_stt_lock; "
        "import sys; c=global_stt_lock(Path(sys.argv[1])); "
        "held=c.__enter__(); print('held' if held else 'busy'); c.__exit__(None,None,None)"
    )
    with global_stt_lock(lock) as held:
        assert held
        child = subprocess.run(
            [sys.executable, "-c", script, str(lock)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert child.stdout.strip() == "busy"


def test_worker_mount_paths_cannot_inject_mount_options(tmp_path):
    with pytest.raises(ValueError, match="unsafe operator path"):
        worker_argv(
            image="registry.example.invalid/stt@sha256:" + "a" * 64,
            input_path=tmp_path / "input,ro=false",
            output_path=tmp_path / "output",
            model_path=tmp_path / "model",
            seccomp_path=tmp_path / "seccomp.json",
        )


def test_resource_admission_uses_separate_stt_and_evaluator_budgets():
    from sessionscribe.dispatch import Resources

    small_worker = Resources(1024 * 1024**2, 256 * 1024**2, 0.5, 4)
    assert small_worker.admits("analysis")
    assert not small_worker.admits("stt")
    assert not Resources(512 * 1024**2, 256 * 1024**2, 0.5, 4).admits("analysis")
    assert not Resources(1024 * 1024**2, 64 * 1024**2, 0.5, 4).admits("analysis")
    assert not Resources(1024 * 1024**2, 256 * 1024**2, 4, 4).admits("analysis")
    with pytest.raises(ValueError):
        small_worker.admits("unregistered-worker")
