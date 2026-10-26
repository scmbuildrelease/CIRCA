"""Compact prompts for optional local explanations."""

from .models import AnalysisResult, CIFailure, HistoricalIncident

# Upper bound on extracted log text sent to a local LLM; whole lines are kept.
MAX_PROMPT_LOG_CHARS = 3000
MAX_PROMPT_REFERENCES = 3


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


def _bounded_lines(text: str, limit: int = MAX_PROMPT_LOG_CHARS) -> str:
    kept: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def build_diagnosis_prompt(failure: CIFailure, result: AnalysisResult, references: list[HistoricalIncident]) -> str:
    """Build a bounded prompt for a structured diagnosis hypothesis.

    Only extracted failure lines, any supplied environment, the deterministic
    result, and a few weak historical references are included; never the raw log.
    """

    sections = [
        (
            "Diagnose this CI failure using only the supplied log lines. Return JSON only, with fields: "
            "category (one of hardware, software, network, infrastructure, unknown), confidence (0 to 1), "
            "root_cause, evidence (log lines quoted verbatim), suggested_fix, validation_steps (concrete checks "
            "that would confirm the fix), missing_evidence. The root cause is a hypothesis; use unknown and a low "
            "confidence when evidence is insufficient. Do not invent facts."
        ),
        f"Log lines:\n{_bounded_lines(failure.failure_text)}",
        f"Deterministic analysis: {result.category} ({result.confidence:.2f}); {result.root_cause}",
    ]
    if failure.environment:
        sections.append("Environment: " + ", ".join(f"{key}={value}" for key, value in failure.environment.items()))
    if references:
        weak = "; ".join(f"{item.id}: {item.title}" for item in references[:MAX_PROMPT_REFERENCES])
        sections.append(f"Weak historical matches (not confirmed): {weak}")
    return "\n".join(sections)
