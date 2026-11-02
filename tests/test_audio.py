import io
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from conftest import start

from sessionscribe.models import SessionState
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.transcript import load_chunks
from sessionscribe.voice import DiscordReceiver, ReceiveUnavailable, RtpTimeline, RtpTimingDrop


def test_lossless_bounded_segments_preserve_overlap_silence_loss_rejoin(rig):
    start(rig)
    pcm = np.arange(FRAME_BYTES // 2, dtype=np.int16).tobytes()
    for t in range(0, 1200, 20):
        assert rig.recorder.accept(Frame(rig.actor.user_id, t, pcm))
        if 400 <= t < 800:
            assert rig.recorder.accept(Frame(rig.other.user_id, t, pcm))
    assert not rig.recorder.accept(Frame(rig.actor.user_id, 1000, pcm))
    rig.recorder.accept(Frame(rig.actor.user_id, 2000, pcm))
    rig.control.pause(rig.actor, rig.session.id)
    rig.control.membership(rig.session.id, frozenset({rig.actor.user_id}))
    rig.control.membership(rig.session.id, rig.session.participants)
    rig.control.alias_consent(rig.other, rig.session.id, "Ben", accepted=True)
    rig.control.consent(rig.other, rig.session.id, cloud=True)
    rig.control.resume(rig.actor, rig.session.id)
    rig.recorder.accept(Frame(rig.other.user_id, 4000, pcm))
    rig.control.stop(rig.actor, rig.session.id)
    chunks = load_chunks(rig.store, rig.session.id)
    assert [(c.speaker_id, c.start_ms, c.end_ms) for c in chunks] == [
        (rig.actor.user_id, 0, 1000),
        (rig.other.user_id, 400, 800),
        (rig.actor.user_id, 1000, 1200),
        (rig.actor.user_id, 2000, 2020),
        (rig.other.user_id, 4000, 4020),
    ]
    assert chunks[3].gap_before_ms == 800
    assert chunks[4].gap_before_ms == 3200
    for chunk in chunks:
        raw = rig.store.files.read(f"sessions/{rig.session.id}/{chunk.path}")
        decoded, rate = sf.read(io.BytesIO(raw), dtype="int16", always_2d=True)
        assert rate == 48000
        expected = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
        np.testing.assert_array_equal(
            decoded, np.tile(expected, ((chunk.end_ms - chunk.start_ms) // 20, 1))
        )
        assert len(raw) < 200_000


def test_rtp_wrap_and_new_connection_use_shared_clock():
    timeline = RtpTimeline()
    assert timeline.timestamp("one", 2**32 - 960, 100) == 100
    assert timeline.timestamp("one", 0, 120) == 120
    assert timeline.timestamp("two", 12345, 400) == 400
    assert timeline.timestamp("rejoin", 42, 4000) == 4000
    with pytest.raises(ValueError):
        timeline.timestamp("one", 960 * 500, 100)


def test_rejected_rtp_packets_do_not_change_the_anchor_and_connections_stay_bounded():
    timeline = RtpTimeline()
    assert timeline.timestamp("one", 48_000, 20) == 20
    for timestamp, reason in ((0, "before_session"), (48_000 * 30, "ahead_of_arrival")):
        with pytest.raises(RtpTimingDrop) as rejected:
            timeline.timestamp("one", timestamp, 40)
        assert rejected.value.reason == reason
    assert timeline.timestamp("one", 48_960, 40) == 40
    for index in range(63):
        assert timeline.timestamp(str(index), 0, 60) == 60
    with pytest.raises(ValueError, match="bounded"):
        timeline.timestamp("overflow", 0, 60)
    assert len(timeline.anchors) == 64


@pytest.mark.parametrize("limit", ["disk", "quota", "duration"])
def test_resource_stops_keep_completed_audio(rig, monkeypatch, limit):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    if limit == "disk":
        monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    elif limit == "quota":
        rig.recorder.bytes_written = rig.config.limits.max_audio_bytes
    if limit == "duration":
        rig.recorder.tick(rig.config.limits.max_duration_ms)
    else:
        assert not rig.recorder.accept(Frame(rig.actor.user_id, 20, bytes(FRAME_BYTES)))
    assert rig.store.session(rig.session.id).state == SessionState.INTERRUPTED
    assert len(load_chunks(rig.store, rig.session.id)) == 1


def test_unfinished_audio_is_detected_and_never_auto_processed(rig):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.store.interrupt_recordings()
    with pytest.raises(ValueError, match="incomplete"):
        load_chunks(rig.store, rig.session.id)


def test_recorder_import_never_loads_models():
    code = (
        "import sessionscribe.recorder,sys; "
        "assert not any(n in sys.modules for n in "
        "('torch','faster_whisper','ctranslate2','onnxruntime'))"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_unreviewed_discord_receive_fails_closed():
    with pytest.raises(ReceiveUnavailable):
        DiscordReceiver().connect()


def test_fragment_count_is_bounded_even_for_tiny_gapped_packets(rig):
    start(rig)
    rig.recorder.limits = rig.config.limits.model_copy(update={"max_chunks": 1})
    assert rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    assert not rig.recorder.accept(Frame(rig.actor.user_id, 40, bytes(FRAME_BYTES)))
    assert rig.recorder.failure == "chunk_limit"
    assert len(load_chunks(rig.store, rig.session.id)) == 1
