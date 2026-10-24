"""Deterministic normalization and fingerprinting for CI failures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping

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


# Volatile-field rules for deduplication only, applied in this order. Exit codes,
# HTTP status codes, ports, and other small literal numbers are kept because
# they distinguish materially different failures.
_VOLATILE_PATTERNS = (
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?\b"),
        "<timestamp>",
    ),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b"), "<address>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b(?=[^0-9a-z]|$)"), "<hash>"),
    (re.compile(r"(?<![\w/])/(?:tmp|var/tmp|private/var/folders)/\S+"), "<temp-path>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|msec|s|sec|secs|seconds|m|min|mins|minutes)\b"), "<duration>"),
    (re.compile(r"(?<=[a-z][-_])\d+\b"), "<n>"),
    (re.compile(r"(?:#|\b(?:build|run|job|pid|attempt)[ :#-]?)\d+\b"), "<id>"),
)
_LOG_LEVEL_PREFIX = re.compile(r"^(?:\[[a-z]+\]|[a-z]+:)\s*")


def normalize_volatile_text(text: str) -> str:
    """Normalize one evidence line for deduplication, removing volatile values."""

    normalized = text.lower().strip()
    for pattern, replacement in _VOLATILE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    # Drop a leading timestamp placeholder and log-level label so prefixes from
    # different CI log formats do not create distinct fingerprints.
    normalized = normalized.removeprefix("<timestamp>").removeprefix("<time>").strip()
    return _LOG_LEVEL_PREFIX.sub("", normalized)


def dedup_fingerprint(evidence: Iterable[str]) -> str:
    """Return a SHA-256 deduplication fingerprint for sanitized evidence lines.

    Separate from :func:`failure_fingerprint`, whose output is unchanged. Lines
    are normalized, deduplicated, and sorted, so line order, timestamps, and
    other volatile values do not affect the result.
    """

    lines = sorted({line for line in (normalize_volatile_text(item) for item in evidence) if line})
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
