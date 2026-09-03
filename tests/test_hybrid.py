import pytest

from circa.hybrid import apply_hybrid_scoring
from circa.models import AnalysisResult, LLMClassification


def _result(
    *, category: str = "hardware", confidence: float = 0.70, matches: list[str] | None = None
) -> AnalysisResult:
    return AnalysisResult(
        fingerprint="abc",
        category=category,
        confidence=confidence,
        root_cause="Deterministic root cause.",
        recommendation="Investigate the worker.",
        matched_incident_ids=matches or [],
    )


def _classification(category: str = "hardware", confidence: float = 0.90) -> LLMClassification:
    return LLMClassification(
        category=category,
        confidence=confidence,
        root_cause="Unsupported LLM guess.",
        evidence=["LLM evidence"],
    )


def test_hybrid_scoring_modestly_increases_confidence_on_agreement() -> None:
    hybrid = apply_hybrid_scoring(_result(matches=["incident-1"]), _classification())

    assert hybrid.final_category == "hardware"
    assert hybrid.final_confidence == pytest.approx(0.775)
    assert hybrid.final_root_cause == "Deterministic root cause."
    assert hybrid.llm_classification is not None


def test_hybrid_scoring_does_not_select_disagreeing_llm_category() -> None:
    hybrid = apply_hybrid_scoring(_result(matches=["incident-1"]), _classification("network"))

    assert hybrid.final_category == "hardware"
    assert hybrid.final_confidence == pytest.approx(0.73)
    assert hybrid.final_root_cause == "Deterministic root cause."


def test_hybrid_scoring_keeps_weak_evidence_unknown() -> None:
    hybrid = apply_hybrid_scoring(_result(category="unknown", confidence=0.0), _classification())

    assert hybrid.final_category == "unknown"
    assert hybrid.final_confidence == 0.0


def test_hybrid_scoring_without_llm_preserves_deterministic_result() -> None:
    result = _result(matches=["incident-1"])

    assert apply_hybrid_scoring(result, None) is result


def test_historical_match_increases_hybrid_confidence() -> None:
    no_match = apply_hybrid_scoring(_result(), _classification("network"))
    with_match = apply_hybrid_scoring(_result(matches=["incident-1"]), _classification("network"))

    assert with_match.final_confidence == pytest.approx(no_match.final_confidence + 0.03)


def test_hybrid_confidence_is_capped_at_one() -> None:
    hybrid = apply_hybrid_scoring(_result(confidence=0.99, matches=["incident-1"]), _classification(confidence=1.0))

    assert hybrid.final_confidence == 1.0
