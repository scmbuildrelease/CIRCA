"""Small local pipeline from raw CI logs to deterministic analysis."""

from __future__ import annotations

from collections.abc import Mapping

from .analyzer import analyze_failure
from .log_parser import extract_failure_text
from .models import AnalysisResult, CIFailure


def analyze_log(raw_log: str, environment: Mapping[str, str] | None = None) -> AnalysisResult:
    """Extract failure evidence from a raw log and analyze it locally."""

    failure_text = extract_failure_text(raw_log) or "No recognized failure evidence in CI log."
    failure = CIFailure(failure_text=failure_text, environment=dict(environment or {}))
    return analyze_failure(failure)
