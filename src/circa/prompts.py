"""Compact prompts for optional local explanations."""

from .models import AnalysisResult


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
