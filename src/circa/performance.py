"""Deterministic evidence extraction for explicit performance-threshold failures."""

from __future__ import annotations

import re

from .models import PerformanceAssessment, PerformanceMeasurement

_EXPLICIT_VIOLATION = re.compile(r"\bperformance\s+threshold\s+violation\b", re.IGNORECASE)
_METRIC_VIOLATION = re.compile(
    r"\b(?:latency|throughput|bandwidth|runtime|duration|fps|performance)\b.*"
    r"\b(?:exceeds?|violat(?:es|ion)|regression|outside)\b",
    re.IGNORECASE,
)
_MEASUREMENT = re.compile(
    r"(?P<metric>[A-Za-z_][\w.-]*)\s+"
    r"(?:actual|observed|measured|value)\s*=\s*(?P<actual>\d+(?:\.\d+)?)\s*"
    r"(?P<actual_unit>ns|us|µs|ms|s|fps|ops/s|GB/s|%)?"
    r".*?\b(?:expected|limit|threshold|allowed)\s*(?P<operator><=|<|>=|>|==)\s*"
    r"(?P<expected>\d+(?:\.\d+)?)\s*(?P<expected_unit>ns|us|µs|ms|s|fps|ops/s|GB/s|%)?",
    re.IGNORECASE,
)
_TOLERANCE = re.compile(r"\btolerance\s*=\s*(?P<value>\d+(?:\.\d+)?\s*(?:%|ns|us|µs|ms|s)?)", re.IGNORECASE)
_TEST_DETAIL = re.compile(r"\b(?:test|benchmark|suite)\s*[:=]\s*(?P<value>[A-Za-z0-9_.-]+)", re.IGNORECASE)
_DRIFT_SIGNAL = re.compile(
    r"\b(?:firmware(?:[_\s-]version)?|microcode|bios|driver(?:[_\s-]version)?|hardware[_\s-]revision)\b",
    re.IGNORECASE,
)
_BASELINE_SIGNAL = re.compile(r"\b(?:baseline|reference[_\s-]run)\b", re.IGNORECASE)
_TEST_CONFIGURATION_SIGNAL = re.compile(r"\b(?:test[_\s-]config(?:uration)?|configuration)\b", re.IGNORECASE)
# Source code shown by tools is not test output. `gh run view --log` prefixes
# lines with "job<TAB>step<TAB>" and a timestamp; only that prefix is removed, so
# a diff context line keeps its leading space.
_LOG_PREFIX = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?\ufeff?(?:\d{4}-\d{2}-\d{2}T[\d:.]+Z ?)?")
_DIFF_HEADER = re.compile(r"^(?:--- \S|\+\+\+ \S|@@ .* @@)")
_DIFF_BODY = re.compile(r"^(?:[-+ ]|$)")
_SNIPPET_START = re.compile(r"^\s*-->\s")
_SNIPPET_BODY = re.compile(r"^(?:\s*$|\s*(?:\d+\s*)?[|+-](?:\s|$)|(?:help|note):)")
_BREACHED = {
    "<=": lambda actual, expected: actual > expected,
    "<": lambda actual, expected: actual >= expected,
    ">=": lambda actual, expected: actual < expected,
    ">": lambda actual, expected: actual <= expected,
    "==": lambda actual, expected: actual != expected,
}


def _output_lines(raw_log: str) -> list[str]:
    """Return log lines that are tool or test output, without diffs and lint code snippets."""

    lines: list[str] = []
    in_diff = in_snippet = False
    for raw_line in raw_log.splitlines():
        text = _LOG_PREFIX.sub("", raw_line, count=1)
        if in_snippet and _SNIPPET_BODY.match(text):
            continue
        in_snippet = bool(_SNIPPET_START.match(text))
        if in_snippet:
            continue
        if _DIFF_HEADER.match(text):
            in_diff = True
            continue
        if in_diff and _DIFF_BODY.match(text):
            continue
        in_diff = False
        if raw_line.strip():
            lines.append(raw_line.strip())
    return lines


def analyze_performance_failure(raw_log: str) -> PerformanceAssessment | None:
    """Assess explicit threshold evidence without changing deterministic analysis.

    A generic assertion is intentionally ignored. Firmware or hardware wording
    only produces a drift *hypothesis*; it never confirms a root cause or
    changes a performance baseline.
    """

    lines = _output_lines(raw_log)
    has_violation = any(_EXPLICIT_VIOLATION.search(line) or _METRIC_VIOLATION.search(line) for line in lines)
    if not has_violation:
        return None

    measurements: list[PerformanceMeasurement] = []
    has_breach = False
    test_details: list[str] = []
    evidence: list[str] = []
    has_drift_signal = False
    has_baseline_signal = False
    has_test_configuration = False
    for line in lines:
        measurement = _MEASUREMENT.search(line)
        if measurement:
            actual_unit = measurement.group("actual_unit") or measurement.group("expected_unit") or None
            expected_unit = measurement.group("expected_unit") or actual_unit or ""
            tolerance = _TOLERANCE.search(line)
            measurements.append(
                PerformanceMeasurement(
                    metric=measurement.group("metric"),
                    actual_value=float(measurement.group("actual")),
                    expected_range=f"{measurement.group('operator')} {measurement.group('expected')} {expected_unit}".strip(),
                    unit=actual_unit,
                    tolerance=tolerance.group("value") if tolerance else None,
                )
            )
            evidence.append(line)
            # Only a value that crosses its stated threshold (in the same unit) shows a regression.
            same_unit = not (measurement.group("actual_unit") and measurement.group("expected_unit")) or (
                measurement.group("actual_unit").lower() == measurement.group("expected_unit").lower()
            )
            has_breach = has_breach or (
                same_unit
                and _BREACHED[measurement.group("operator")](
                    float(measurement.group("actual")), float(measurement.group("expected"))
                )
            )
        detail = _TEST_DETAIL.search(line)
        if detail:
            test_details.append(detail.group("value"))
        if _DRIFT_SIGNAL.search(line):
            has_drift_signal = True
            evidence.append(line)
        has_baseline_signal = has_baseline_signal or bool(_BASELINE_SIGNAL.search(line))
        has_test_configuration = has_test_configuration or bool(_TEST_CONFIGURATION_SIGNAL.search(line))

    missing_evidence: list[str] = []
    if not measurements:
        missing_evidence.append("No explicit metric value and expected threshold were found.")
    elif not has_breach:
        missing_evidence.append("No measured value was found beyond its expected threshold.")
    if not test_details:
        missing_evidence.append("No explicit test or benchmark identifier was found.")
    if not has_drift_signal:
        missing_evidence.append("No hardware or firmware version evidence was found.")
    if not has_baseline_signal:
        missing_evidence.append("No explicit approved baseline reference was found.")
    if not has_test_configuration:
        missing_evidence.append("No explicit test configuration was found.")

    if has_drift_signal:
        candidate_cause = "hardware_firmware_performance_drift"
        recommended_action = "performance_threshold_recalibration"
        recommendation = (
            "Compare repeated measurements with the approved baseline, hardware and firmware versions, and test "
            "configuration. Consider performance-threshold recalibration only after human validation and approval; "
            "do not change thresholds or baselines automatically."
        )
    elif has_breach:
        candidate_cause = "possible_genuine_performance_regression"
        recommended_action = None
        recommendation = (
            "Investigate the measured regression against the approved baseline and test configuration. Confirm the "
            "measurement before deciding whether code, environment, or threshold changes are warranted."
        )
    else:
        candidate_cause = "undetermined"
        recommended_action = None
        recommendation = (
            "Collect the metric value, expected threshold, tolerance, baseline, versions, and test configuration "
            "before determining a cause. Do not change thresholds or baselines automatically."
        )

    return PerformanceAssessment(
        failure_type="performance_threshold_violation",
        candidate_cause=candidate_cause,
        recommended_action=recommended_action,
        measurements=measurements,
        test_details=sorted(set(test_details)),
        evidence=list(dict.fromkeys(evidence)),
        missing_evidence=missing_evidence,
        recommendation=recommendation,
    )
