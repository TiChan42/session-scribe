"""Bounded lossless FLAC writer. No inference imports, whole-call buffers or network calls."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass

import numpy as np
import soundfile as sf

from .models import AudioChunk, Limits, SessionState
from .store import Store

RATE = 48_000
CHANNELS = 2
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * CHANNELS * 2


@dataclass(frozen=True)
class Frame:
    speaker_id: str
    start_ms: int
    pcm: bytes


@dataclass
class OpenChunk:
    id: str
    speaker: str
    start: int
    end: int
    gap: int
    file: object
    encoder: sf.SoundFile


class Recorder:
    def __init__(self, store: Store, sid: str, limits: Limits):
        self.store, self.sid, self.limits = store, sid, limits
        store.session(sid)
        self.open: dict[str, OpenChunk] = {}
        self.last_end: dict[str, int] = {}
        self.chunk_count = 0
        self.bytes_written = 0
        self.failure: str | None = None

    def _fail(self, reason: str):
        self.failure = reason
        s = self.store.session(self.sid)
        self.store.save_session(
            s, s.model_copy(update={"state": SessionState.INTERRUPTED, "stop_reason": reason})
        )
        self.close()

    def tick(self, elapsed_ms: int):
        if elapsed_ms >= self.limits.max_duration_ms:
            self._fail("duration_limit")

    def accept(self, frame: Frame) -> bool:
        s = self.store.session(self.sid)
        if s.state != SessionState.RECORDING or self.failure:
            return False
        if frame.speaker_id not in s.participants or not s.participants <= s.consented:
            self._fail("unconfirmed_participant")
            return False
        if (
            type(frame.start_ms) is not int
            or frame.start_ms < 0
            or frame.start_ms % FRAME_MS
            or len(frame.pcm) != FRAME_BYTES
        ):
            raise ValueError("expected one timestamped 20ms PCM16 stereo frame")
        if frame.start_ms + FRAME_MS > self.limits.max_duration_ms:
            self._fail("duration_limit")
            return False
        if frame.start_ms < self.last_end.get(frame.speaker_id, 0):
            return False  # Duplicate/reordered frames are never appended twice.
        # Conservative PCM reservation bounds lossless FLAC including headers.
        reserve = FRAME_BYTES + 128
        if (
            self.bytes_written + reserve > self.limits.max_audio_bytes
            or shutil.disk_usage(self.store.files.root).free < self.limits.min_free_bytes + reserve
        ):
            self._fail("storage_limit")
            return False
        chunk = self.open.get(frame.speaker_id)
        try:
            if chunk and (
                frame.start_ms != chunk.end or chunk.end - chunk.start >= self.limits.segment_ms
            ):
                self._close_speaker(frame.speaker_id)
                chunk = None
            if chunk is None:
                if self.chunk_count + len(self.open) >= self.limits.max_chunks:
                    self._fail("chunk_limit")
                    return False
                cid = uuid.uuid4().hex
                relative = f"sessions/{self.sid}/audio/{cid}.part"
                output = self.store.files.create(relative)
                encoder = sf.SoundFile(
                    output,
                    mode="w",
                    samplerate=RATE,
                    channels=CHANNELS,
                    format="FLAC",
                    subtype="PCM_16",
                    closefd=False,
                )
                chunk = OpenChunk(
                    cid,
                    frame.speaker_id,
                    frame.start_ms,
                    frame.start_ms,
                    max(0, frame.start_ms - self.last_end.get(frame.speaker_id, 0)),
                    output,
                    encoder,
                )
                self.open[frame.speaker_id] = chunk
                # Journal is durable before the first PCM frame, for crash recovery.
                self.store.files.json(
                    f"sessions/{self.sid}/audio/{cid}.journal.json",
                    {
                        "speaker_id": frame.speaker_id,
                        "start_ms": frame.start_ms,
                        "status": "incomplete",
                    },
                )
            chunk.encoder.write(np.frombuffer(frame.pcm, dtype="<i2").reshape(-1, CHANNELS))
            chunk.encoder.flush()
            chunk.end = frame.start_ms + FRAME_MS
            self.last_end[frame.speaker_id] = chunk.end
            self.bytes_written += reserve
            return True
        except OSError:
            self._fail("audio_io_failure")
            return False

    def _close_speaker(self, speaker: str):
        chunk = self.open.pop(speaker)
        chunk.encoder.close()
        chunk.file.flush()
        os.fsync(chunk.file.fileno())
        chunk.file.close()
        base = f"sessions/{self.sid}/audio/{chunk.id}"
        self.store.files.rename(base + ".part", base + ".flac")
        checksum = self.store.files.sha256(
            base + ".flac", limit=self.limits.segment_ms * 200 + 8192
        )
        metadata = AudioChunk(
            id=chunk.id,
            session_id=self.sid,
            speaker_id=speaker,
            start_ms=chunk.start,
            end_ms=chunk.end,
            path=f"audio/{chunk.id}.flac",
            sha256=checksum,
            gap_before_ms=chunk.gap,
        )
        self.store.files.json(base + ".json", metadata.model_dump(mode="json"))
        self.chunk_count += 1
        self.store.files.remove(base + ".journal.json")

    def close(self):
        for speaker in list(self.open):
            self._close_speaker(speaker)
