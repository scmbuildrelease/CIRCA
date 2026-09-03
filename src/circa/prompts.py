"""Compact prompts for optional local explanations."""

from .models import AnalysisResult, CIFailure


def build_explanation_prompt(failure_text: str, result: AnalysisResult) -> str:
    """Build a bounded prompt from extracted evidence and deterministic output."""

    matches = ", ".join(result.matched_incident_ids) or "none"
    evidence = "; ".join(result.evidence) or "none"
    return (
        "Explain this CI failure concisely using only the supplied facts. "
        f"The deterministic category is '{result.category}' and must not be changed. "
        "Do not invent facts, diagnoses, or actions.\n"
        f"Extracted failure: {failure_text}\n"
        f"Deterministic root cause: {result.root_cause}\n"
        f"Deterministic evidence: {evidence}\n"
        f"Historical matches: {matches}\n"
        "State the likely root cause and the supporting evidence."
    )


def build_classification_prompt(failure: CIFailure, result: AnalysisResult) -> str:
    """Build a bounded prompt for an independent structured classification."""

    environment = ", ".join(f"{key}={value}" for key, value in failure.environment.items()) or "none"
    matches = ", ".join(result.matched_incident_ids) or "none"
    return (
        "Independently classify this CI failure using only the supplied facts. "
        "Return JSON only, with exactly these fields: category, confidence, root_cause, evidence. "
        "Choose exactly one category from hardware, software, network, infrastructure, unknown. "
        "Confidence must be a number from 0 to 1. Do not invent evidence. Use unknown when evidence "
        "is insufficient. Keep root_cause concise and each evidence item short.\n"
        f"Extracted failure text: {failure.failure_text}\n"
        f"Environment: {environment}\n"
        f"Historical matches: {matches}"
    )
