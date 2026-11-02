"""Validation for participant-chosen aliases shown in private guild protocols."""

from __future__ import annotations

import unicodedata


def validate_alias(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("alias must be text")
    alias = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(alias) <= 32:
        raise ValueError("alias length must be between 1 and 32 characters")
    if any(unicodedata.category(character).startswith("C") for character in alias):
        raise ValueError("alias contains control or formatting characters")
    if any(character in alias for character in "@<>`|\\"):
        raise ValueError("alias contains a reserved character")
    return alias
