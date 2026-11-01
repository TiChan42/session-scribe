from __future__ import annotations

import json

from .models import AudioChunk, Segment
from .store import Store


def canonicalize(segments: list[Segment], session_id: str) -> list[Segment]:
    seen: dict[str, Segment] = {}
    for segment in segments:
        if segment.session_id != session_id:
            raise ValueError("cross-session transcript")
        if segment.id in seen and seen[segment.id] != segment:
            raise ValueError("conflicting segment ID")
        seen[segment.id] = segment
    # Text equality alone is never deduplication: simultaneous speech and repeats matter.
    return sorted(seen.values(), key=lambda s: (s.start_ms, s.end_ms, s.speaker_id, s.id))


def load_chunks(store: Store, sid: str) -> list[AudioChunk]:
    store.session(sid)
    directory = store.files.root / "sessions" / sid / "audio"
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("missing audio directory")
    if list(directory.glob("*.part")) or list(directory.glob("*.journal.json")):
        raise ValueError("incomplete audio needs operator review")
    chunks = []
    for path in sorted(directory.glob("*.json")):
        chunk = AudioChunk.model_validate_json(
            store.files.read(f"sessions/{sid}/audio/{path.name}", 8192)
        )
        if chunk.session_id != sid or path.stem != chunk.id:
            raise ValueError("audio manifest mismatch")
        chunks.append(chunk)
    if not chunks or len(chunks) > 100_000:
        raise ValueError("invalid chunk count")
    return sorted(chunks, key=lambda c: (c.start_ms, c.speaker_id, c.id))


def save_transcript(store: Store, sid: str, segments: list[Segment]):
    ordered = canonicalize(segments, sid)
    store.files.json(
        f"sessions/{sid}/transcript.json", [s.model_dump(mode="json") for s in ordered]
    )


def load_transcript(store: Store, sid: str) -> list[Segment]:
    store.session(sid)
    data = json.loads(store.artifact(sid, "transcript.json"))
    if not isinstance(data, list) or len(data) > 50_000:
        raise ValueError("invalid transcript count")
    return canonicalize([Segment.model_validate(s) for s in data], sid)
