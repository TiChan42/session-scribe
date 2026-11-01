import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessionscribe.models import InferenceSettings
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.store import Store
from sessionscribe.voice_test import DiskCapture, TestPolicy, qualification_sink


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


def packet(timestamp, pcm=None):
    return SimpleNamespace(
        packet=SimpleNamespace(ssrc=501, timestamp=timestamp, decrypted_data=b"synthetic-opus"),
        pcm=pcm if pcm is not None else b"\x01\x00" * (FRAME_BYTES // 2),
    )


def test_timing_outliers_do_not_stop_valid_audio_or_reanchor_its_gaps(tmp_path):
    capture = DiskCapture(policy(), lambda *_args: "synthetic-visible-receipt", tmp_path)
    capture.start()
    try:
        assert capture.ready.wait(5) and capture.failure is None
        capture.started_at -= 0.2
        state = SimpleNamespace(ssrc_user_map={501: 1003}, dave_session=SimpleNamespace(ready=True))
        sink = qualification_sink(state, capture, lambda: True)
        first = 2**32 - 960
        sink.write(packet(first), None)
        sink.write(packet((first - 48_000 * 2) % 2**32), None)
        sink.write(packet((first + 48_000 * 20) % 2**32), None)
        sink.write(packet(0), None)
        sink.write(packet(960 * 4), None)
        until = time.monotonic() + 5
        while capture.frame_count < 3 and time.monotonic() < until:
            time.sleep(0.01)
        assert capture.failure is None and not capture.stop_requested.is_set()
    finally:
        capture.stop_requested.set()
        capture.join(5)
    receipt = capture.receipt
    assert receipt["frames"] == receipt["nonzero_frames"] == 3
    assert receipt["timing_drops"] == {"before_session": 1, "ahead_of_arrival": 1}
    assert receipt["dave_single_speaker_receive"] is True
    assert receipt["production_voice_qualified"] is False
    store = Store(Path(receipt["root"]), "1000")
    try:
        from sessionscribe.transcript import load_chunks

        chunks = load_chunks(store, receipt["session_id"])
        assert len(chunks) == 2
        assert chunks[0].end_ms - chunks[0].start_ms == 40
        assert chunks[1].gap_before_ms == 60
        assert store.queue_status()[0]["kind"] == "stt"
    finally:
        store.close()


@pytest.mark.parametrize("violation", ["member", "ssrc", "user", "dave", "encrypted", "pcm", "rtp"])
def test_sink_still_stops_for_authorization_encryption_and_malformed_frames(violation):
    capture = DiskCapture(policy(), lambda *_args: None)
    capture.ready.set()
    capture.started_at = time.monotonic()
    state = SimpleNamespace(
        ssrc_user_map={501: 1004 if violation == "ssrc" else 1003},
        dave_session=SimpleNamespace(ready=violation != "dave"),
    )
    data = packet(True if violation == "rtp" else 100)
    if violation == "encrypted":
        data.packet.decrypted_data = b""
    if violation == "pcm":
        data.pcm = b"invalid-frame"
    sink = qualification_sink(state, capture, lambda: violation != "member")
    sink.write(data, SimpleNamespace(id=1004) if violation == "user" else None)
    assert capture.stop_requested.is_set() and capture.failure is not None
    assert capture.frames.empty()
