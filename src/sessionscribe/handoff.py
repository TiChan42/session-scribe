"""Versioned worker data contracts. No process execution, network or inference imports."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import AudioChunk, Config, Identifier, Segment, Session, StrictModel, Tenant

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Phase = Literal["stt", "analysis"]


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def session_data(session: Session) -> dict:
    data = session.model_dump(mode="json")
    for key in ("participants", "attendees", "consented", "cloud_consent"):
        data[key] = sorted(data[key])
    data["speaker_aliases"] = dict(sorted(data["speaker_aliases"].items()))
    return data


def config_data(config: Config) -> dict:
    data = config.model_dump(mode="json")
    for key in ("command_channel_ids", "voice_channel_ids", "recorder_bot_ids"):
        data[key] = sorted(data[key])
    for grant in data["grants"].values():
        for key in ("users", "roles"):
            grant[key] = sorted(grant[key])
    return data


class Binding(StrictModel):
    tenant: Tenant
    phase: Phase
    job_id: Identifier
    token: Identifier
    input_sha256: Digest

    @property
    def attempt(self) -> str:
        return f"{self.job_id}-{self.token}"


class SttHandoff(StrictModel):
    schema_version: Literal[1]
    session_id: Identifier
    chunks: list[AudioChunk] = Field(min_length=1, max_length=50_000)
    # Unbound inputs are accepted for explicit local engine smoke tests only.
    # A broker always requires matching bindings in the final output.
    binding: Binding | None = None

    @model_validator(mode="after")
    def valid_chunks(self):
        seen, last_end = set(), {}
        for chunk in sorted(self.chunks, key=lambda c: (c.start_ms, c.speaker_id, c.id)):
            if (
                chunk.session_id != self.session_id
                or chunk.id in seen
                or chunk.path != f"audio/{chunk.id}.flac"
                or chunk.start_ms < last_end.get(chunk.speaker_id, 0)
            ):
                raise ValueError("duplicate, overlapping or cross-session audio manifest")
            seen.add(chunk.id)
            last_end[chunk.speaker_id] = chunk.end_ms
        if self.binding and self.binding.phase != "stt":
            raise ValueError("wrong handoff phase")
        return self


class AnalysisHandoff(StrictModel):
    schema_version: Literal[1]
    session: Session
    transcript_sha256: Digest
    binding: Binding | None = None

    @model_validator(mode="after")
    def valid_binding(self):
        if self.binding and (
            self.binding.phase != "analysis" or self.binding.tenant != self.session.tenant
        ):
            raise ValueError("wrong evaluation binding")
        return self


class Completion(StrictModel):
    schema_version: Literal[1]
    session_id: Identifier
    binding: Binding | None
    outputs: dict[str, Digest] = Field(min_length=1, max_length=4)
    model_revision: str | None = None
    compute_type: Literal["int8"] | None = None


class Checkpoint(StrictModel):
    audio_sha256: Digest
    segments: list[Segment] = Field(max_length=1000)

    def validate_chunk(self, chunk: AudioChunk) -> list[Segment]:
        if self.audio_sha256 != chunk.sha256:
            raise ValueError("stale STT checkpoint")
        seen = set()
        for s in self.segments:
            if (
                s.session_id != chunk.session_id
                or not s.id.startswith(chunk.id + ":")
                or int(s.id.split(":")[1]) >= 1000
                or s.id in seen
                or s.speaker_id != chunk.speaker_id
                or s.start_ms < chunk.start_ms
                or s.end_ms > chunk.end_ms
                or s.gap_before_ms != (chunk.gap_before_ms if not seen else 0)
            ):
                raise ValueError("invalid STT checkpoint")
            seen.add(s.id)
        return self.segments


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
