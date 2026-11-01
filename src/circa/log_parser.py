"""Deterministic extraction of useful failure evidence from CI logs."""

from __future__ import annotations

import re

_FAILURE_SIGNAL = re.compile(
    r"\b(?:\w*error|failed|failure|\w*exception|traceback|timeout|threshold violation|"
    r"connection refused|no space left|out of memory|oom|segmentation fault)\b|\bnpm ERR!"
    # Quality-gate summaries that state a failure without the word "error".
    r"|\b[1-9]\d* files? would be reformatted\b|\bwould reformat\b|\bcode style issues found\b|>> Issue: \[B\d{3}:"
    r"|\bfound [1-9]\d* errors?\b|\S\.\w+:\d+:\d+: [A-Z]{1,3}\d{3,4}\b",
    re.IGNORECASE,
)
# A line that starts its own log record (timestamp, clock time, bracketed tag, or LEVEL:).
_RECORD_START = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\[|[A-Z]{3,}:)")
_REPEATED_PREFIX = re.compile(r"^(?:\[[^\]]+\]\s*)?(?:info|debug|notice)\b", re.IGNORECASE)
# `gh run view --log` adds "job<TAB>step<TAB>" and a timestamp before each line's own text.
_LOG_PREFIX = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?\ufeff?(?:\d{4}-\d{2}-\d{2}T\S+(?: |$))?")
_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
# Bandit prints each finding's source after its location, as "<line number><TAB><code>".
_BANDIT_LOCATION = re.compile(r"^\s*Location: \S+:\d+:\d+")
_NUMBERED_SOURCE = re.compile(r"^\d+\t")


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


def _printed_source_lines(raw_lines: list[str]) -> list[bool]:
    """Mark source text a tool printed, not log output.

    That is unified-diff headers and hunks (``ruff format --diff``) and the numbered code
    snippet Bandit prints after each finding's location.
    """

    texts = [_LOG_PREFIX.sub("", line, count=1) for line in raw_lines]
    marks = [False] * len(texts)
    in_hunk = in_snippet = False
    for index, text in enumerate(texts):
        if _HUNK_HEADER.match(text):
            in_hunk = marks[index] = True
        elif text.startswith("--- ") and index + 1 < len(texts) and texts[index + 1].startswith("+++ "):
            in_hunk, marks[index], marks[index + 1] = False, True, True
        elif in_hunk and (not text or text[0] in " +-") or in_snippet and _NUMBERED_SOURCE.match(text):
            marks[index] = True
        elif not marks[index]:
            in_hunk = False
            in_snippet = _BANDIT_LOCATION.match(text) is not None
    return marks


def extract_failure_text(raw_log: str, context_before: int = 1, context_after: int = 2) -> str:
    """Return compact failure evidence, including nearby non-repetitive context.

    The parser selects lines with known failure signals and keeps a small
    surrounding window. Exact duplicate lines and repeated informational lines
    are omitted to avoid carrying noisy progress output into a ``CIFailure``.
    """

    # Printed source is excluded: an error name or "timeout" in source code is not something that occurred.
    raw_lines = raw_log.splitlines()
    lines = [
        line.strip()
        for line, source in zip(raw_lines, _printed_source_lines(raw_lines), strict=True)
        if line.strip() and not source
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
