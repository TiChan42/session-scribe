import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from conftest import start

from sessionscribe import model_cache
from sessionscribe.files import SafeFiles
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.stt import run_handoff, transcribe_chunk
from sessionscribe.transcript import load_chunks


def test_restart_reuses_chunk_checkpoints_without_loading_another_model(rig, tmp_path, monkeypatch):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.recorder.accept(Frame(rig.other.user_id, 0, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    chunks = load_chunks(rig.store, rig.session.id)
    inputs = SafeFiles(tmp_path / "input")
    for chunk in chunks:
        inputs.atomic(
            f"sessions/{rig.session.id}/{chunk.path}",
            rig.store.files.read(f"sessions/{rig.session.id}/{chunk.path}"),
        )
    inputs.json(
        "handoff.json",
        {
            "schema_version": 1,
            "session_id": rig.session.id,
            "chunks": [c.model_dump(mode="json") for c in chunks],
        },
    )
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"synthetic model stand-in")
    digest = hashlib.sha256((model / "model.bin").read_bytes()).hexdigest()
    monkeypatch.setattr(model_cache, "SHA256", {"model.bin": digest})
    (model / "provenance.json").write_text(
        json.dumps(
            {
                "repository": model_cache.REPOSITORY,
                "revision": model_cache.REVISION,
                "sha256": model_cache.SHA256,
            }
        )
    )
    constructors = []

    class Model:
        def __init__(self, *_args, **kwargs):
            constructors.append(kwargs)
            assert kwargs["compute_type"] == "int8" and kwargs["device"] == "cpu"
            assert kwargs["local_files_only"] is True and kwargs["cpu_threads"] == 2

        def transcribe(self, _raw, **kwargs):
            assert kwargs["language"] is None
            return iter([SimpleNamespace(start=0, end=0.02, text="synthetic speech")]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    output = tmp_path / "output"
    run_handoff(inputs.root, output, model)
    assert len(constructors) == 1
    assert len(json.loads((output / "transcript.json").read_text())) == 2
    run_handoff(inputs.root, output, model)
    assert len(constructors) == 1
    (model / "model.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        run_handoff(inputs.root, output, model)


def test_audio_hash_and_session_are_checked_before_decode(rig):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    chunk = load_chunks(rig.store, rig.session.id)[0]
    with pytest.raises(ValueError, match="cross-session"):
        transcribe_chunk(None, rig.store.files, "b" * 32, chunk)
    with pytest.raises(ValueError, match="integrity"):
        transcribe_chunk(
            None, rig.store.files, rig.session.id, chunk.model_copy(update={"sha256": "0" * 64})
        )
