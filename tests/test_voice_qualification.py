import json
import time
from pathlib import Path

from sessionscribe.models import InferenceSettings
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.store import Store
from sessionscribe.voice_test import DiskCapture, TestPolicy


def policy():
    return TestPolicy(
        purpose="authorized-single-person-dave-test",
        guild_id="1000",
        voice_channel_id="1001",
        protocol_channel_id="1002",
        participant_id="1003",
        expires_at=int(time.time()) + 300,
        cloud_consent=True,
        inference=InferenceSettings(
            model="synthetic-model", context_tokens=8192, output_tokens=2048
        ),
    )


def test_test_capture_keeps_durable_consent_segments_and_queue(tmp_path):
    capture = DiskCapture(policy(), lambda *_args: "synthetic-visible-receipt", tmp_path)
    capture.start()
    try:
        assert capture.ready.wait(5) and capture.failure is None
        for index in range(2):
            capture.accept(Frame("1003", index * 20, b"\x01\x00" * (FRAME_BYTES // 2)))
        until = time.monotonic() + 5
        while capture.frame_count < 2 and time.monotonic() < until:
            time.sleep(0.01)
    finally:
        capture.stop_requested.set()
        capture.join(5)
    assert not capture.is_alive() and capture.receipt["frames"] == 2
    assert capture.receipt["production_voice_qualified"] is False
    root = Path(capture.receipt["root"])
    assert list(root.glob("sessions/*/audio/*.flac"))
    with_store = Store(root, "1000")
    try:
        session = with_store.session(capture.receipt["session_id"])
        assert session.consented == session.cloud_consent == frozenset({"1003"})
        assert with_store.queue_status()[0]["kind"] == "stt"
    finally:
        with_store.close()
    assert json.loads((root / "test-authorization.json").read_text())["cloud_consent"] is True


def test_audio_queue_overflow_closes_gate_without_unbounded_growth():
    capture = DiskCapture(policy(), lambda *_args: None)
    capture.ready.set()
    for index in range(300):
        capture.accept(Frame("1003", index * 20, bytes(FRAME_BYTES)))
    assert capture.stop_requested.is_set()
    assert capture.failure == "bounded_audio_queue_full"
    assert capture.frames.qsize() == 256
