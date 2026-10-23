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
        "PERFORMANCE THRESHOLD VIOLATION\n"
        "latency_p99 actual=21 ms expected<=15 ms\n"
        "firmware_version=2.0.1"
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
