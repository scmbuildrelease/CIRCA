"""Deterministic hybrid scoring for optional local LLM classifications."""

from .models import AnalysisResult, LLMClassification

HISTORICAL_MATCH_BONUS = 0.03
LLM_AGREEMENT_WEIGHT = 0.05


def apply_hybrid_scoring(result: AnalysisResult, llm_classification: LLMClassification | None) -> AnalysisResult:
    """Attach an independent LLM opinion without overriding deterministic analysis.

    Historical matches add 0.03 confidence. Matching LLM categories add up to
    0.05, scaled by the LLM confidence. The deterministic category and root
    cause always remain authoritative; an unknown deterministic result stays
    unknown.
    """

    if llm_classification is None:
        return result

    confidence = result.confidence
    if result.category != "unknown":
        if result.matched_incident_ids:
            confidence += HISTORICAL_MATCH_BONUS
        if llm_classification.category == result.category:
            confidence += LLM_AGREEMENT_WEIGHT * llm_classification.confidence

    return result.model_copy(
        update={
            "llm_classification": llm_classification,
            "final_category": result.category,
            "final_confidence": min(1.0, confidence),
            "final_root_cause": result.root_cause,
        }
    )
