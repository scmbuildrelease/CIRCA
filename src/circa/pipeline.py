"""Small local pipeline from raw CI logs to deterministic analysis."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from .analyzer import analyze_failure
from .fingerprint import dedup_fingerprint, normalize_volatile_text
from .knowledge import find_similar_incident_matches, tokenize
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
# The analyzer's minimum: a claim must share at least two distinctive terms with the log.
_MIN_GROUNDING_TERMS = 2
# Suggestions that would weaken a safeguard rather than fix a cause; humans must decide these.
_SAFEGUARD_WEAKENING = re.compile(
    r"\b(?:disabl\w*|bypass\w*|skip\w*|ignor\w*|suppress\w*|unsigned|--force|force[- ]push\w*|"
    r"--no-verify|threshold\w*|baseline\w*)\b",
    re.IGNORECASE,
)


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


def _terms(text: str) -> set[str]:
    """Distinctive terms without sentence punctuation (``tokenize`` keeps dots for versions and paths)."""

    terms = {token.strip("./-") for token in tokenize(text)}
    # A plain plural matches its singular ("rules" and "rule"); applied to both sides of every comparison.
    return {
        term[:-1] if len(term) > 3 and term.endswith("s") and not term.endswith("ss") else term
        for term in terms
        if term
    }


def _grounded(text: str, log_tokens: set[str]) -> bool:
    return len(_terms(text) & log_tokens) >= _MIN_GROUNDING_TERMS


def _log_quote(quote: str, lines: list[str], log_tokens: set[str]) -> bool:
    """A model citation counts as log evidence only if it is part of one actual log line."""

    text = quote.strip()
    return bool(text) and any(text in line for line in lines) and _grounded(text, log_tokens)


def _supported_suggestions(
    diagnosis: LLMDiagnosis, supporting_terms: set[str]
) -> tuple[str | None, list[str], list[str]]:
    """Keep the suggested fix and validation steps only if they keep safeguards and relate to the evidence.

    ``supporting_terms`` are the log's terms plus those of the historical incidents
    the deterministic analyzer matched, so a fix may use the history's wording.
    """

    def problem(text: str) -> str | None:
        if _SAFEGUARD_WEAKENING.search(text):
            return "would weaken a safeguard"
        return None if _grounded(text, supporting_terms) else "is unrelated to the log or matched history"

    notes: list[str] = []
    fix = (diagnosis.suggested_fix or "").strip() or None
    if fix is not None and (reason := problem(fix)):
        notes.append(f"Suggested fix omitted from the draft because it {reason}: {fix}")
        fix = None
    steps: list[str] = []
    for step in diagnosis.validation_steps:
        if reason := problem(step):
            notes.append(f"Validation step omitted from the draft because it {reason}: {step}")
        else:
            steps.append(step)
    return fix, steps, notes


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
    log_tokens = _terms(failure.failure_text)
    cited = [line.strip() for line in diagnosis.evidence if _log_quote(line, lines, log_tokens)]
    if len(cited) < len(diagnosis.evidence):
        notes.append(f"Ignored {len(diagnosis.evidence) - len(cited)} LLM-cited line(s) not found in the log.")
    diagnosis = diagnosis.model_copy(update={"evidence": cited})
    # Deterministic analysis stays authoritative; the LLM category is used only when it found none.
    category: CauseCategory
    if analysis.category != "unknown":
        category = analysis.category
        category_source = "historical" if analysis.matched_incident_ids else "deterministic"
        if diagnosis.category != analysis.category:
            notes.append(f"LLM suggested {diagnosis.category}; keeping deterministic category {analysis.category}.")
    else:
        category, category_source = diagnosis.category, "llm"
    # Each check is independent; there is no confidence cut-off beyond the model's own zero.
    rejections = [
        reason
        for failed, reason in (
            (category == "unknown", "no category was identified"),
            (diagnosis.confidence == 0.0, "the LLM reported zero confidence"),
            (not _grounded(diagnosis.root_cause, log_tokens), "the root cause is not grounded in the log"),
            (not cited, "the LLM quoted no line that appears in the log"),
        )
        if failed
    ]
    if rejections:
        notes += [f"No draft created: {'; '.join(rejections)}.", unknowns]
        return Investigation(outcome="deterministic_only", llm_called=True, diagnosis=diagnosis, notes=notes, **base)
    history_terms = {
        term
        for incident in references
        for term in _terms(" ".join([incident.title, incident.summary, *incident.keywords]))
    }
    fix, steps, suggestion_notes = _supported_suggestions(diagnosis, log_tokens | history_terms)
    notes += suggestion_notes

    draft: dict[str, Any] = {
        "id": f"draft-{fingerprint[:12]}",
        "title": _clip(evidence[0], 120),
        "category": category,
        "summary": _clip(" ".join(evidence)),
        "recommendation": fix or analysis.recommendation,
        "synthetic": synthetic,
        "evidence": evidence,
        "root_cause": diagnosis.root_cause,
        "draft_origin": {
            "llm_model": provider.model,
            "source_log": source_name,
            "confidence": diagnosis.confidence,
            "validation_steps": steps,
            "missing_evidence": diagnosis.missing_evidence,
            "related_incident_ids": analysis.matched_incident_ids,
            "category_source": category_source,
            "cited_log_lines": cited,
        },
    }
    if fix:
        draft["fix"] = {"description": fix, "validation_status": "unvalidated"}
    return Investigation(
        outcome="new_diagnosis", llm_called=True, diagnosis=diagnosis, draft=draft, notes=notes, **base
    )
