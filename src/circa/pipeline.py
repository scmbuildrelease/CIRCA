"""Small local pipeline from raw CI logs to deterministic analysis."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from .analyzer import analyze_failure
from .fingerprint import dedup_fingerprint, normalize_volatile_text
from .knowledge import find_similar_incident_matches
from .knowledge_store import NEAR_DUPLICATE_MIN_SCORE, analysis_incidents, find_near_duplicates
from .llm import DiagnosisProvider, diagnose_failure
from .log_parser import extract_failure_text, has_failure_signal
from .models import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_LINES,
    AnalysisResult,
    CauseCategory,
    CIFailure,
    HistoricalIncident,
    LLMDiagnosis,
    contains_secret,
)
from .performance import analyze_performance_failure

_NO_EVIDENCE = "No recognized failure evidence in CI log."


def analyze_log(
    raw_log: str,
    environment: Mapping[str, str] | None = None,
    incidents: Iterable[HistoricalIncident] | None = None,
) -> AnalysisResult:
    """Extract failure evidence from a raw log and analyze it locally.

    ``incidents`` defaults to the packaged synthetic incidents.
    """

    failure_text = extract_failure_text(raw_log) or _NO_EVIDENCE
    failure = CIFailure(failure_text=failure_text, environment=dict(environment or {}))
    result = analyze_failure(failure, incidents)
    return result.model_copy(update={"performance_assessment": analyze_performance_failure(raw_log)})


InvestigationOutcome = Literal[
    "known_solution", "existing_record", "new_diagnosis", "deterministic_only", "no_failure_evidence"
]


class Investigation(BaseModel):
    """The result of one end-to-end log investigation; it never writes knowledge itself."""

    outcome: InvestigationOutcome
    analysis: AnalysisResult
    evidence: list[str] = Field(default_factory=list)
    dedup_fingerprint: str | None = None
    matched_incident: HistoricalIncident | None = None
    match_reason: str | None = None
    llm_called: bool = False
    diagnosis: LLMDiagnosis | None = None
    draft: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _sanitized_lines(failure_text: str) -> list[str]:
    """Drop credential-looking lines and volatile-only repeats of the same line."""

    lines: list[str] = []
    seen: set[str] = set()
    for line in failure_text.splitlines():
        key = normalize_volatile_text(line)
        if contains_secret(line) or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return lines


def _known_solution(
    failure: CIFailure, fingerprint: str, analysis: AnalysisResult, incidents: list[HistoricalIncident]
) -> tuple[HistoricalIncident, str] | None:
    """Return a verified incident with a validated fix that strongly matches, if any."""

    def has_validated_fix(incident: HistoricalIncident) -> bool:
        return (
            incident.status == "verified" and incident.fix is not None and incident.fix.validation_status == "validated"
        )

    for incident in incidents:
        if fingerprint in incident.fingerprints and has_validated_fix(incident):
            return incident, "same failure fingerprint"
    for match in find_similar_incident_matches(failure, incidents):
        # Reuses the analyzer's ranking plus the existing near-duplicate threshold;
        # the match must also agree with the deterministic category.
        if (
            match.score >= NEAR_DUPLICATE_MIN_SCORE
            and match.incident.id in analysis.matched_incident_ids
            and has_validated_fix(match.incident)
        ):
            return match.incident, f"keyword score {match.score:g}"
    return None


def _existing_record(
    evidence: list[str], fingerprint: str, category: CauseCategory, team_incidents: list[HistoricalIncident]
) -> tuple[HistoricalIncident, str] | None:
    for incident in team_incidents:
        if fingerprint in incident.fingerprints:
            return incident, "same failure fingerprint"
    probe = CIFailure(failure_text=" ".join(evidence), category=None if category == "unknown" else category)
    near = find_near_duplicates(probe, team_incidents)
    return (near[0].incident, f"near duplicate, keyword score {near[0].score:g}") if near else None


def investigate_log(
    raw_log: str,
    team_incidents: list[HistoricalIncident],
    provider: DiagnosisProvider | None,
    *,
    source_name: str,
    synthetic: bool = False,
) -> Investigation:
    """Investigate a raw log: verified history first, then at most one local LLM call.

    Returns a proposed ``pending_review`` draft only for a useful new diagnosis;
    persisting it is left to the caller.
    """

    incidents = analysis_incidents(team_incidents)
    analysis = analyze_log(raw_log, incidents=incidents)
    lines = _sanitized_lines(extract_failure_text(raw_log))
    evidence = [_clip(line) for line in lines if has_failure_signal(line)][:MAX_EVIDENCE_LINES]
    if not evidence:
        return Investigation(
            outcome="no_failure_evidence",
            analysis=analysis,
            notes=["No recognized failure lines in the log; the LLM was not called and no draft was created."],
        )

    fingerprint = dedup_fingerprint(evidence)
    failure = CIFailure(failure_text="\n".join(lines))
    base = {"analysis": analysis, "evidence": evidence, "dedup_fingerprint": fingerprint}
    known = _known_solution(failure, fingerprint, analysis, incidents)
    if known is not None:
        return Investigation(outcome="known_solution", matched_incident=known[0], match_reason=known[1], **base)
    existing = _existing_record(evidence, fingerprint, analysis.category, team_incidents)
    if existing is not None:
        incident, reason = existing
        return Investigation(
            outcome="existing_record",
            matched_incident=incident,
            match_reason=reason,
            notes=[f"Already recorded as {incident.status} incident '{incident.id}'; no LLM call and no new draft."],
            **base,
        )

    unknowns = "The root cause remains unconfirmed; collect more failure output before drafting."
    if provider is None:
        return Investigation(outcome="deterministic_only", notes=["Local LLM not used.", unknowns], **base)
    references = [incident for incident in incidents if incident.id in analysis.matched_incident_ids]
    diagnosis = diagnose_failure(failure, analysis, references, provider)
    if diagnosis is None:
        return Investigation(
            outcome="deterministic_only",
            llm_called=True,
            notes=[
                "Local LLM unavailable or returned an invalid response; showing deterministic analysis only.",
                unknowns,
            ],
            **base,
        )

    notes: list[str] = []
    cited = [line for line in diagnosis.evidence if line.strip() and line.strip() in failure.failure_text]
    if len(cited) < len(diagnosis.evidence):
        notes.append(f"Ignored {len(diagnosis.evidence) - len(cited)} LLM-cited line(s) not found in the log.")
    diagnosis = diagnosis.model_copy(update={"evidence": cited})
    # Deterministic analysis stays authoritative; the LLM category is used only when it found none.
    category = analysis.category if analysis.category != "unknown" else diagnosis.category
    if analysis.category != "unknown" and diagnosis.category != analysis.category:
        notes.append(f"LLM suggested {diagnosis.category}; keeping deterministic category {analysis.category}.")
    if category == "unknown" or diagnosis.confidence == 0.0:
        notes += ["The LLM could not identify a cause with any confidence; no draft was created.", unknowns]
        return Investigation(outcome="deterministic_only", llm_called=True, diagnosis=diagnosis, notes=notes, **base)

    draft: dict[str, Any] = {
        "id": f"draft-{fingerprint[:12]}",
        "title": _clip(evidence[0], 120),
        "category": category,
        "summary": _clip(" ".join(evidence)),
        "recommendation": diagnosis.suggested_fix or analysis.recommendation,
        "synthetic": synthetic,
        "evidence": evidence,
        "root_cause": diagnosis.root_cause,
        "draft_origin": {
            "llm_model": provider.model,
            "source_log": source_name,
            "confidence": diagnosis.confidence,
            "validation_steps": diagnosis.validation_steps,
            "missing_evidence": diagnosis.missing_evidence,
            "related_incident_ids": analysis.matched_incident_ids,
        },
    }
    if diagnosis.suggested_fix:
        draft["fix"] = {"description": diagnosis.suggested_fix, "validation_status": "unvalidated"}
    return Investigation(
        outcome="new_diagnosis", llm_called=True, diagnosis=diagnosis, draft=draft, notes=notes, **base
    )
