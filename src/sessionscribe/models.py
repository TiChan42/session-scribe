from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Snowflake = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Mode = Literal["meeting", "dnd"]
Tenant = Snowflake  # Discord guild ID; the mode is not the data boundary.


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SessionState(StrEnum):
    WAITING = "waiting_consent"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    DELETED = "deleted"


class InferenceSettings(StrictModel):
    model: str = Field(min_length=1, max_length=150)
    prompt_version: Literal["v1"] = "v1"
    context_tokens: int = Field(ge=1024, le=262144)
    output_tokens: int = Field(ge=256, le=16384)
    overhead_tokens: int = Field(default=512, ge=256)
    temperature: float = Field(default=0.0, ge=0, le=1)
    # Omitted legacy controls retain their original serialized snapshot and request.
    thinking: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    split_mode: Literal["reject"] = "reject"

    @model_validator(mode="after")
    def fits(self):
        if self.output_tokens + self.overhead_tokens >= self.context_tokens:
            raise ValueError("context must leave room for input")
        return self


class Limits(StrictModel):
    max_participants: int = Field(default=8, ge=1, le=32)
    max_duration_ms: int = Field(default=14_400_000, ge=1000, le=28_800_000)
    segment_ms: int = Field(default=15_000, ge=20, le=30_000, multiple_of=20)
    max_chunks: int = Field(default=10_000, ge=1, le=50_000)
    max_audio_bytes: int = Field(default=1_073_741_824, ge=8192)
    min_free_bytes: int = Field(default=536_870_912, ge=0)


class Grant(StrictModel):
    users: frozenset[Snowflake] = frozenset()
    roles: frozenset[Snowflake] = frozenset()


class Config(StrictModel):
    mode: Mode
    guild_id: Snowflake
    command_channel_ids: frozenset[Snowflake]
    voice_channel_ids: frozenset[Snowflake]
    output_channel_id: Snowflake  # Visible recording status; protocol delivery is separate.
    default_protocol_channel_id: Snowflake | None = None
    voice_protocol_channels: dict[Snowflake, Snowflake | None] = Field(default_factory=dict)
    recorder_bot_ids: frozenset[Snowflake] = frozenset()
    grants: dict[str, Grant]
    limits: Limits = Limits()
    inference: InferenceSettings
    recording_enabled: bool = False
    cloud_enabled: bool = False
    daily_cloud_calls: int = Field(default=4, ge=1, le=100)
    daily_cloud_tokens: int = Field(default=100_000, ge=1024, le=10_000_000)
    # Explicit player/character mapping; multiple characters per Discord connection.
    characters: dict[Snowflake, list[str]] = Field(default_factory=dict)
    campaign: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid_protocol_routes(self):
        if set(self.voice_protocol_channels) - self.voice_channel_ids:
            raise ValueError("protocol route requires an allowed voice channel")
        return self

    def protocol_channel(self, voice_channel_id: str) -> str | None:
        if voice_channel_id not in self.voice_channel_ids:
            raise ValueError("voice channel not configured")
        return self.voice_protocol_channels.get(voice_channel_id, self.default_protocol_channel_id)

    @property
    def tenant(self) -> str:
        return self.guild_id


class Actor(StrictModel):
    guild_id: Snowflake
    channel_id: Snowflake
    user_id: Snowflake
    role_ids: frozenset[Snowflake] = frozenset()


class Session(StrictModel):
    id: Identifier
    tenant: Tenant
    mode: Mode
    owner_id: Snowflake
    voice_channel_id: Snowflake
    recorder_bot_id: Snowflake | None = None
    protocol_channel_id: Snowflake | None = None
    status_channel_id: Snowflake | None = None
    state: SessionState = SessionState.WAITING
    participants: frozenset[Snowflake]
    attendees: frozenset[Snowflake] = frozenset()
    consented: frozenset[Snowflake] = frozenset()
    cloud_consent: frozenset[Snowflake] = frozenset()
    inference: InferenceSettings
    characters: dict[Snowflake, list[str]] = Field(default_factory=dict)
    campaign: str | None = None
    stop_reason: str | None = None


class AudioChunk(StrictModel):
    id: Identifier
    session_id: Identifier
    speaker_id: Snowflake
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    path: str = Field(pattern=r"^audio/[a-f0-9]{32}\.flac$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    gap_before_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_interval(self):
        if not 0 < self.end_ms - self.start_ms <= 30_000:
            raise ValueError("invalid audio interval")
        return self


class Segment(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}:[0-9]{4,8}$")
    session_id: Identifier
    speaker_id: Snowflake
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=8000)
    gap_before_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("invalid transcript interval")
        return self


class Finding(StrictModel):
    kind: Literal["topic", "decision", "proposal", "task", "question", "chronicle", "summary"]
    text: str = Field(min_length=1, max_length=4000)
    sources: list[str] = Field(min_length=1, max_length=100)
    owner: str | None
    deadline: str | None
    category: Literal["story", "mechanics", "planning", "meta", "unclear"] | None
    character: str | None


class Analysis(StrictModel):
    schema_version: Literal[1]
    tenant: Tenant
    mode: Mode
    session_id: Identifier
    findings: list[Finding] = Field(max_length=500)
