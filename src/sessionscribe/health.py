"""Small, content-free control readiness check, bound to the expected revision."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .files import SafeFiles
from .models import StrictModel


class ControlHealth(StrictModel):
    schema_version: Literal[1]
    revision: str = Field(pattern=r"^(development|[a-f0-9]{40,64})$")
    checked_at: float = Field(ge=0, allow_inf_nan=False)
    control_ready: bool
    voice_ready: bool
    cloud_ready: bool


def control_ready(root: Path, revision: str, *, now: float | None = None) -> bool:
    try:
        if not root.is_dir():
            return False
        health = ControlHealth.model_validate_json(
            SafeFiles(root).read("control-health.json", limit=4096)
        )
        current = time.time() if now is None else now
        return (
            health.revision == revision
            and health.control_ready
            and 0 <= current - health.checked_at <= 65
        )
    except (OSError, ValueError):
        return False
