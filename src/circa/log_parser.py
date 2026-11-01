"""Deterministic extraction of useful failure evidence from CI logs."""

from __future__ import annotations

import re

_FAILURE_SIGNAL = re.compile(
    r"\b(?:\w*error|failed|failure|\w*exception|traceback|timeout|threshold violation|"
    r"connection refused|no space left|out of memory|oom|segmentation fault)\b|\bnpm ERR!",
    re.IGNORECASE,
)
# A line that starts its own log record (timestamp, clock time, bracketed tag, or LEVEL:).
_RECORD_START = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\[|[A-Z]{3,}:)")
_REPEATED_PREFIX = re.compile(r"^(?:\[[^\]]+\]\s*)?(?:info|debug|notice)\b", re.IGNORECASE)
# `gh run view --log` adds "job<TAB>step<TAB>" and a timestamp before each line's own text.
_LOG_PREFIX = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?\ufeff?(?:\d{4}-\d{2}-\d{2}T\S+(?: |$))?")
_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def has_failure_signal(line: str) -> bool:
    """Return whether a log line contains a recognized failure signal."""

    return _FAILURE_SIGNAL.search(line) is not None


def _starts_record(line: str) -> bool:
    # `gh run view --log` prefixes every line with "job<TAB>step<TAB>", and a
    # byte-order mark may precede the timestamp; check the record text itself.
    text = line.split("\t", 2)[-1] if line.count("\t") >= 2 else line
    return _RECORD_START.match(text.lstrip("\ufeff")) is not None


def failure_evidence_lines(raw_log: str) -> list[str]:
    """Return exact log lines that state a failure, in order and without duplicates.

    A failure-signal line is kept with its immediate continuation: the next
    adjacent line when it does not start a log record of its own, such as the
    second line of a multi-line error message. Nothing is inferred or reworded.
    """

    raw_lines = raw_log.splitlines()
    selected: list[str] = []
    for index, line in enumerate(raw_lines):
        if not has_failure_signal(line):
            continue
        selected.append(line.strip())
        following = raw_lines[index + 1].strip() if index + 1 < len(raw_lines) else ""
        if following and not has_failure_signal(following) and not _starts_record(following):
            selected.append(following)
    return list(dict.fromkeys(selected))


def _diff_lines(raw_lines: list[str]) -> list[bool]:
    """Mark unified-diff headers and hunk lines: source text a tool printed (``ruff format --diff``), not log output."""

    texts = [_LOG_PREFIX.sub("", line, count=1) for line in raw_lines]
    marks = [False] * len(texts)
    in_hunk = False
    for index, text in enumerate(texts):
        if _HUNK_HEADER.match(text):
            in_hunk = marks[index] = True
        elif text.startswith("--- ") and index + 1 < len(texts) and texts[index + 1].startswith("+++ "):
            in_hunk, marks[index], marks[index + 1] = False, True, True
        elif in_hunk and (not text or text[0] in " +-"):
            marks[index] = True
        elif not marks[index]:
            in_hunk = False
    return marks


def extract_failure_text(raw_log: str, context_before: int = 1, context_after: int = 2) -> str:
    """Return compact failure evidence, including nearby non-repetitive context.

    The parser selects lines with known failure signals and keeps a small
    surrounding window. Exact duplicate lines and repeated informational lines
    are omitted to avoid carrying noisy progress output into a ``CIFailure``.
    """

    # Diff content is excluded: an error name in printed source code is not an error that occurred.
    raw_lines = raw_log.splitlines()
    lines = [
        line.strip() for line, diff in zip(raw_lines, _diff_lines(raw_lines), strict=True) if line.strip() and not diff
    ]
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
