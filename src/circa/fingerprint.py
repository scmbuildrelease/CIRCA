"""Deterministic normalization and fingerprinting for CI failures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .models import CIFailure

_WHITESPACE = re.compile(r"\s+")
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\b\d{3,}\b")


def normalize_failure_text(text: str) -> str:
    """Remove volatile details while preserving meaningful failure words."""

    normalized = text.lower().strip()
    normalized = _HEX_ADDRESS.sub("<address>", normalized)
    normalized = _LONG_NUMBER.sub("<number>", normalized)
    return _WHITESPACE.sub(" ", normalized)


def normalize_environment(environment: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Return environment data in a stable, case-insensitive form."""

    return tuple(sorted((str(key).strip().lower(), str(value).strip().lower()) for key, value in environment.items()))


def failure_fingerprint(failure: CIFailure) -> str:
    """Produce a stable SHA-256 fingerprint for equivalent local failures."""

    environment = "|".join(f"{key}={value}" for key, value in normalize_environment(failure.environment))
    category = failure.category or ""
    material = f"text={normalize_failure_text(failure.failure_text)}|environment={environment}|category={category}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
