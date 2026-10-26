from pathlib import Path

from circa.log_parser import extract_failure_text, failure_evidence_lines


def test_extracts_clear_failure_from_large_log() -> None:
    log = "\n".join(["INFO compiling package" for _ in range(100)])
    log += "\nERROR: no space left on device\ncompiler output could not be written\njob failed"

    extracted = extract_failure_text(log)

    assert "ERROR: no space left on device" in extracted
    assert "compiler output could not be written" in extracted
    assert extracted.count("INFO compiling package") <= 1


def test_includes_context_around_failure_line() -> None:
    log = "setup complete\nstarting integration test\nTraceback (most recent call last):\nAssertionError: expected 2\ncleanup started"

    extracted = extract_failure_text(log)

    assert "starting integration test" in extracted
    assert "Traceback (most recent call last):" in extracted
    assert "AssertionError: expected 2" in extracted
    assert "cleanup started" in extracted


def test_returns_empty_text_when_no_failure_signal_is_present() -> None:
    assert extract_failure_text("build started\nall tasks complete\nexit code 0") == ""


def test_extracts_multiple_distinct_errors() -> None:
    log = "connecting to registry\nconnection refused by registry\nretrying\nERROR: package download FAILED\nfinished"

    extracted = extract_failure_text(log)

    assert "connection refused by registry" in extracted
    assert "ERROR: package download FAILED" in extracted


def test_extracts_named_exception_lines() -> None:
    log = "Traceback (most recent call last):\n  File a.py\n  File b.py\n  File c.py\nKeyError: 'build_id'"

    assert "KeyError: 'build_id'" in extract_failure_text(log)
    assert "java.lang.NullPointerException" in extract_failure_text("java.lang.NullPointerException at Foo")


def test_extracts_explicit_performance_threshold_violation() -> None:
    assert "PERFORMANCE THRESHOLD VIOLATION" in extract_failure_text("PERFORMANCE THRESHOLD VIOLATION\nlatency high")


PROTECTED_TAG_LOG = Path(__file__).parent / "fixtures" / "real_incidents" / "tt-metal-protected-tag.log"


def test_failure_evidence_keeps_the_continuation_of_a_multi_line_error() -> None:
    # Sanitized public tt-metal release failure: the second line has no failure keyword.
    assert failure_evidence_lines(PROTECTED_TAG_LOG.read_text()) == [
        "HttpError: Repository rule violations found",
        "Cannot update this protected ref.",
    ]


def test_failure_evidence_does_not_absorb_unrelated_lines() -> None:
    lines = (
        "Cannot reach the cache; continuing without it",  # no failure signal, not after one
        "2026-10-22T10:00:00Z [ERROR] upload failed",
        "2026-10-22T10:00:01Z [INFO] next record, not a continuation",
        "ERROR: second failure",
        "",
        "detached text after a blank line",
        "ERROR: second failure",
    )
    raw_log = "\n".join(lines)

    assert failure_evidence_lines(raw_log) == ["2026-10-22T10:00:00Z [ERROR] upload failed", "ERROR: second failure"]
