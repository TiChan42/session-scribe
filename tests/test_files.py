import os

import pytest
from conftest import start

from sessionscribe.files import SafeFiles
from sessionscribe.recorder import FRAME_BYTES, Frame


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../../b", "a//b", "./a"])
def test_traversal_rejected(tmp_path, path):
    files = SafeFiles(tmp_path / "root")
    with pytest.raises(ValueError):
        files.read(path)


def test_symlink_directory_and_final_file_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("synthetic")
    files = SafeFiles(root)
    (root / "escape").symlink_to(outside)
    (root / "link").symlink_to(outside / "private")
    for path in ("escape/private", "link"):
        with pytest.raises(OSError):
            files.read(path)
    with pytest.raises(ValueError):
        SafeFiles(root / "escape")


def test_special_files_and_oversized_input_rejected(tmp_path):
    files = SafeFiles(tmp_path / "root")
    os.mkfifo(files.root / "fifo")
    with pytest.raises(ValueError, match="regular"):
        files.read("fifo")
    files.atomic("large", b"123456")
    with pytest.raises(ValueError, match="large"):
        files.read("large", 5)


def test_owner_delete_preserves_unrelated_tenant_data(rig, tmp_path):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("synthetic")
    (rig.store.files.root / "sessions" / rig.session.id / "escape").symlink_to(outside)
    rig.control.delete(rig.actor, rig.session.id)
    assert (outside / "keep").exists()
    with pytest.raises(KeyError):
        rig.store.session(rig.session.id)
    assert rig.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    rig.control.delete(rig.actor, rig.session.id)


def test_empty_stopped_session_can_be_deleted(rig):
    rig.control.stop(rig.actor, rig.session.id)
    rig.control.delete(rig.actor, rig.session.id)
    assert rig.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_snapshot_refuses_hard_links_and_bounds_copy_size(tmp_path):
    import hashlib

    source = SafeFiles(tmp_path / "source")
    target = SafeFiles(tmp_path / "target")
    source.atomic("audio", b"fLaCsynthetic")
    checksum = hashlib.sha256(source.read("audio")).hexdigest()
    os.link(source.root / "audio", source.root / "linked")
    with pytest.raises(ValueError, match="unsafe"):
        target.copy_verified(source, "linked", "out", checksum, limit=100, prefix=b"fLaC")
    source.remove("linked")
    with pytest.raises(ValueError, match="unsafe"):
        target.copy_verified(source, "audio", "small", checksum, limit=4, prefix=b"fLaC")
    assert target.copy_verified(source, "audio", "good", checksum, limit=100, prefix=b"fLaC") == 13


def test_data_root_cannot_hide_overlap_with_parent_components(tmp_path):
    with pytest.raises(ValueError, match="parent traversal"):
        SafeFiles(tmp_path / "stage" / ".." / "recorder")


@pytest.mark.parametrize("lock_kind", ["local", "global"])
def test_special_files_cannot_be_used_as_broker_locks(tmp_path, lock_kind):
    from sessionscribe.dispatch import global_stt_lock

    files = SafeFiles(tmp_path / "locks")
    os.mkfifo(files.root / "lock")
    lock = files.lock("lock") if lock_kind == "local" else global_stt_lock(files.root / "lock")
    with pytest.raises(ValueError, match="regular single-link"), lock:
        pytest.fail("special-file lock accepted")
