from circa.analyzer import analyze_failure
from circa.log_parser import extract_failure_text
from circa.models import CIFailure
from circa.performance import analyze_performance_failure
from circa.pipeline import analyze_log


def test_explicit_performance_threshold_violation_extracts_supported_measurement() -> None:
    assessment = analyze_performance_failure(
        "benchmark=attention_latency_p99\n"
        "PERFORMANCE THRESHOLD VIOLATION\n"
        "attention_latency_p99 actual=18.7 ms expected<=15.0 ms tolerance=5%"
    )

    assert assessment is not None
    assert assessment.failure_type == "performance_threshold_violation"
    assert assessment.measurements[0].metric == "attention_latency_p99"
    assert assessment.measurements[0].actual_value == 18.7
    assert assessment.measurements[0].expected_range == "<= 15.0 ms"
    assert assessment.measurements[0].tolerance == "5%"
    assert assessment.test_details == ["attention_latency_p99"]


def test_generic_assertion_is_not_a_performance_failure() -> None:
    assert analyze_performance_failure("AssertionError: expected 2, got 3") is None


def test_incomplete_performance_evidence_reports_missing_details() -> None:
    assessment = analyze_performance_failure("PERFORMANCE THRESHOLD VIOLATION: benchmark check failed")

    assert assessment is not None
    assert assessment.candidate_cause == "undetermined"
    assert assessment.measurements == []
    assert "No explicit metric value and expected threshold were found." in assessment.missing_evidence


def test_firmware_signal_is_a_drift_hypothesis_requiring_human_approval() -> None:
    assessment = analyze_performance_failure(
        "PERFORMANCE THRESHOLD VIOLATION\nlatency_p99 actual=21 ms expected<=15 ms\nfirmware_version=2.0.1"
    )

    assert assessment is not None
    assert assessment.candidate_cause == "hardware_firmware_performance_drift"
    assert assessment.recommended_action == "performance_threshold_recalibration"
    assert assessment.human_validation_required is True
    assert "do not change thresholds or baselines automatically" in assessment.recommendation.lower()


def test_explicit_measurement_without_drift_is_possible_genuine_regression() -> None:
    assessment = analyze_performance_failure("latency_p99 actual=21 ms expected<=15 ms; performance regression")

    assert assessment is not None
    assert assessment.candidate_cause == "possible_genuine_performance_regression"
    assert assessment.recommended_action is None


def test_unrelated_log_noise_is_not_a_performance_failure() -> None:
    assert analyze_performance_failure("INFO cache warmed\nINFO request completed in 12 ms") is None


def test_log_pipeline_attaches_assessment_without_overriding_deterministic_analysis() -> None:
    raw_log = "PERFORMANCE THRESHOLD VIOLATION\nlatency_p99 actual=21 ms expected<=15 ms"
    deterministic = analyze_failure(CIFailure(failure_text=extract_failure_text(raw_log)))
    result = analyze_log(raw_log)

    assert result.performance_assessment is not None
    assert result.performance_assessment.failure_type == "performance_threshold_violation"
    assert result.category == deterministic.category
    assert result.confidence == deterministic.confidence


def _github_log(job: str, step: str, *lines: str) -> str:
    """Format lines the way `gh run view --log` prints them."""

    return "\n".join(f"{job}\t{step}\t2026-11-05T10:00:{index:02d}.0000000Z {line}" for index, line in enumerate(lines))


RUFF_FORMAT_DIFF = _github_log(
    "Lint (Ruff)",
    "Ruff format check",
    "--- src/bench/report.py",
    "+++ src/bench/report.py",
    "@@ -10,7 +10,7 @@",
    "     def report(log):",
    '-        log.error("PERFORMANCE THRESHOLD VIOLATION")',
    "+        log.error(",
    '+            "PERFORMANCE THRESHOLD VIOLATION"',
    "+        )",
    '-    sample = "latency_p99 actual=21 ms expected<=15 ms"',
    "+    sample = 'latency_p99 actual=21 ms expected<=15 ms'",
    "",
    '     firmware_version = "2.0.1"',
    "1 file would be reformatted, 30 files already formatted",
    "##[error]Process completed with exit code 1.",
)

RUFF_LINT_LINES = (
    "E501 Line too long (121 > 120)",
    "  --> src/bench/report.py:14:121",
    "   |",
    '14 |     sample = "latency_p99 actual=21 ms expected<=15 ms; performance regression; firmware_version=2.0.1"',
    "   |                                                                                                    ^",
    "   |",
    "",
    "help: Split the string",
    '   -     log.error("PERFORMANCE THRESHOLD VIOLATION")',
    "15 +     log.error(",
    "",
    "Found 1 error.",
)
RUFF_LINT_FAILURE = "\n".join(RUFF_LINT_LINES)

GENUINE_FAILURE = _github_log(
    "Performance (benchmarks)",
    "Run latency benchmarks",
    "benchmark=attention_latency_p99",
    "PERFORMANCE THRESHOLD VIOLATION",
    "attention_latency_p99 actual=18.7 ms expected<=15.0 ms tolerance=5%",
    "##[error]Process completed with exit code 1.",
)


def test_formatter_diff_of_performance_code_is_not_a_performance_failure() -> None:
    assert analyze_performance_failure(RUFF_FORMAT_DIFF) is None


def test_lint_failure_showing_performance_code_is_not_a_performance_failure() -> None:
    assert analyze_performance_failure(RUFF_LINT_FAILURE) is None
    assert analyze_performance_failure(_github_log("Lint (Ruff)", "Ruff lint", *RUFF_LINT_LINES)) is None


def test_genuine_threshold_failure_in_a_github_log_is_still_recognized() -> None:
    # The same run also printed a formatter diff; only the benchmark output counts.
    assessment = analyze_performance_failure(RUFF_FORMAT_DIFF + "\n" + GENUINE_FAILURE)

    assert assessment is not None
    assert assessment.candidate_cause == "possible_genuine_performance_regression"
    assert [measurement.metric for measurement in assessment.measurements] == ["attention_latency_p99"]
    assert all("Ruff" not in line for line in assessment.evidence)


def test_missing_baseline_and_hardware_evidence_is_reported_not_invented() -> None:
    # firmware_version appears only in the diff, so it is not hardware evidence.
    assessment = analyze_performance_failure(RUFF_FORMAT_DIFF + "\n" + GENUINE_FAILURE)

    assert assessment is not None
    assert assessment.candidate_cause != "hardware_firmware_performance_drift"
    assert assessment.recommended_action is None
    assert "No hardware or firmware version evidence was found." in assessment.missing_evidence
    assert "No explicit approved baseline reference was found." in assessment.missing_evidence


def test_measurement_within_its_threshold_is_not_a_regression() -> None:
    assessment = analyze_performance_failure(
        "PERFORMANCE THRESHOLD VIOLATION reported by suite\nlatency_p99 actual=12 ms expected<=15 ms"
    )

    assert assessment is not None
    assert assessment.candidate_cause == "undetermined"
    assert "No measured value was found beyond its expected threshold." in assessment.missing_evidence
