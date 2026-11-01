"""On-demand worker only. Inference libraries are imported inside the worker entry point."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

from .files import SafeFiles
from .models import AudioChunk, Segment


def transcribe_chunk(model, files: SafeFiles, sid: str, chunk: AudioChunk) -> list[Segment]:
    if chunk.session_id != sid:
        raise ValueError("cross-session audio")
    raw = files.read(f"sessions/{sid}/{chunk.path}", 6_000_000)
    if hashlib.sha256(raw).hexdigest() != chunk.sha256 or not raw.startswith(b"fLaC"):
        raise ValueError("audio integrity or codec mismatch")
    # Input has already passed a tight byte bound; decoded duration is checked before inference.
    import av

    with av.open(io.BytesIO(raw)) as container:
        if len(container.streams.audio) != 1:
            raise ValueError("expected one audio stream")
        stream = container.streams.audio[0]
        if stream.rate != 48_000 or stream.duration is None:
            raise ValueError("invalid audio stream")
        duration = float(stream.duration * stream.time_base)
        if not 0 < duration <= 30.01 or abs(duration * 1000 - (chunk.end_ms - chunk.start_ms)) > 25:
            raise ValueError("audio duration mismatch")
    pieces, _ = model.transcribe(
        io.BytesIO(raw),
        language=None,
        beam_size=3,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    out = []
    for index, part in enumerate(pieces):
        if index >= 1000 or not math.isfinite(part.start) or not math.isfinite(part.end):
            raise ValueError("invalid STT segment")
        text = part.text.strip()
        if not text:
            continue
        start = chunk.start_ms + max(0, round(part.start * 1000))
        end = min(chunk.end_ms, chunk.start_ms + round(part.end * 1000))
        if end <= start:
            continue
        out.append(
            Segment(
                id=f"{chunk.id}:{index:04d}",
                session_id=sid,
                speaker_id=chunk.speaker_id,
                start_ms=start,
                end_ms=end,
                text=text,
                gap_before_ms=chunk.gap_before_ms if not out else 0,
            )
        )
    return out


def run_handoff(input_root: Path, output_root: Path, model_path: Path):
    """Fixed handoff, no credentials, no queue DB, no network and no engine API."""
    from faster_whisper import WhisperModel

    files, output = SafeFiles(input_root), SafeFiles(output_root)
    from .handoff import Checkpoint, Completion, SttHandoff, digest

    handoff = SttHandoff.model_validate_json(files.read("handoff.json"))
    # Only a pre-fetched multilingual small model may be mounted at this path.
    provenance = json.loads((model_path / "provenance.json").read_text())
    from .model_cache import REPOSITORY, REVISION, SHA256

    if (
        provenance.get("repository") != REPOSITORY
        or provenance.get("revision") != REVISION
        or provenance.get("sha256") != SHA256
    ):
        raise ValueError("multilingual small model provenance required")
    for filename, expected_digest in provenance["sha256"].items():
        if "/" in filename or filename.startswith("."):
            raise ValueError("unsafe model manifest")
        with (model_path / filename).open("rb") as model_file:
            actual = hashlib.file_digest(model_file, "sha256").hexdigest()
        if actual != expected_digest:
            raise ValueError("model cache integrity failure")
    model = None
    segments = []
    for chunk in sorted(handoff.chunks, key=lambda c: (c.start_ms, c.speaker_id, c.id)):
        checkpoint = f"chunks/{chunk.id}.json"
        try:
            cached = json.loads(output.read(checkpoint))
        except FileNotFoundError:
            if model is None:
                model = WhisperModel(
                    str(model_path),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=2,
                    num_workers=1,
                    local_files_only=True,
                )
            current = transcribe_chunk(model, files, handoff.session_id, chunk)
            output.json(
                checkpoint,
                {
                    "audio_sha256": chunk.sha256,
                    "segments": [s.model_dump(mode="json") for s in current],
                },
            )
        else:
            current = Checkpoint.model_validate(cached).validate_chunk(chunk)
        segments.extend(current)
    from .transcript import canonicalize

    segments = canonicalize(segments, handoff.session_id)
    output.json("transcript.json", [s.model_dump(mode="json") for s in segments])
    completion = Completion(
        schema_version=1,
        session_id=handoff.session_id,
        binding=handoff.binding,
        outputs={"transcript.json": digest(output.read("transcript.json"))},
        model_revision=provenance["revision"],
        compute_type="int8",
    )
    output.json("complete.json", completion.model_dump(mode="json"))
