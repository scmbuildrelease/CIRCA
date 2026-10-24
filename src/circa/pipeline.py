"""Small local pipeline from raw CI logs to deterministic analysis."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .analyzer import analyze_failure
from .log_parser import extract_failure_text
from .models import AnalysisResult, CIFailure, HistoricalIncident
from .performance import analyze_performance_failure


def analyze_log(
    raw_log: str,
    environment: Mapping[str, str] | None = None,
    incidents: Iterable[HistoricalIncident] | None = None,
) -> AnalysisResult:
    """Extract failure evidence from a raw log and analyze it locally.

    ``incidents`` defaults to the packaged synthetic incidents.
    """

    failure_text = extract_failure_text(raw_log) or "No recognized failure evidence in CI log."
    failure = CIFailure(failure_text=failure_text, environment=dict(environment or {}))
    result = analyze_failure(failure, incidents)
    return result.model_copy(update={"performance_assessment": analyze_performance_failure(raw_log)})
