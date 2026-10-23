"""Deterministic extraction of useful failure evidence from CI logs."""

from __future__ import annotations

import re

_FAILURE_SIGNAL = re.compile(
    r"\b(?:\w*error|failed|failure|\w*exception|traceback|timeout|threshold violation|"
    r"connection refused|no space left|out of memory|oom|segmentation fault)\b",
    re.IGNORECASE,
)
_REPEATED_PREFIX = re.compile(r"^(?:\[[^\]]+\]\s*)?(?:info|debug|notice)\b", re.IGNORECASE)


def extract_failure_text(raw_log: str, context_before: int = 1, context_after: int = 2) -> str:
    """Return compact failure evidence, including nearby non-repetitive context.

    The parser selects lines with known failure signals and keeps a small
    surrounding window. Exact duplicate lines and repeated informational lines
    are omitted to avoid carrying noisy progress output into a ``CIFailure``.
    """

    lines = [line.strip() for line in raw_log.splitlines() if line.strip()]
    failure_indexes = [index for index, line in enumerate(lines) if _FAILURE_SIGNAL.search(line)]
    if not failure_indexes:
        return ""

    selected_indexes: set[int] = set()
    for index in failure_indexes:
        start = max(0, index - max(context_before, 0))
        end = min(len(lines), index + max(context_after, 0) + 1)
        selected_indexes.update(range(start, end))

    extracted: list[str] = []
    seen: set[str] = set()
    for index in sorted(selected_indexes):
        line = lines[index]
        normalized = re.sub(r"\s+", " ", line).lower()
        if normalized in seen:
            continue
        if _REPEATED_PREFIX.search(line) and not _FAILURE_SIGNAL.search(line):
            continue
        seen.add(normalized)
        extracted.append(line)

    return "\n".join(extracted)
