"""Core data models for local CIRCA analysis."""

import re
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

CauseCategory = Literal["hardware", "software", "network", "infrastructure", "unknown"]
IncidentStatus = Literal["pending_review", "verified", "superseded", "rejected"]

MAX_EVIDENCE_LINES = 10
MAX_EVIDENCE_CHARS = 300
MIN_REPEATED_RUNS = 3
# Obvious credential shapes. This is a guard against accidental pastes, not a
# substitute for human review or the repository's Gitleaks scan.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)\s*[:=]\s*"
        r"(?!<redacted>|\*+(?:\s|$))\S+"
    ),
)
_REPEATED_RUNS = re.compile(
    r"\b(\d+)\s+(?:[a-z-]+\s+){0,2}(?:runs|reruns|passes|executions|builds|pipelines|jobs)\b", re.IGNORECASE
)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


class CIFailure(BaseModel):
    """A CI failure and the local context available for matching."""

    failure_text: str = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    category: CauseCategory | None = None


class IncidentFix(BaseModel):
    """The actual fix for an incident and how a human validated it."""

    description: str = Field(min_length=1)
    validation_status: Literal["unvalidated", "validated", "rejected"] = "unvalidated"
    # A single successful retry is deliberately not a validation method.
    validation_method: (
        Literal["reproduced_then_fixed", "regression_test_added", "repeated_runs_passed", "manual_verification"] | None
    ) = None
    validation_evidence: str | None = None
    references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_validation(self) -> Self:
        if self.validation_status == "validated":
            if self.validation_method is None or not (self.validation_evidence or "").strip():
                raise ValueError("a validated fix requires validation_method and validation_evidence")
            if self.validation_method == "repeated_runs_passed":
                runs = _REPEATED_RUNS.search(self.validation_evidence or "")
                if runs is None or int(runs.group(1)) < MIN_REPEATED_RUNS:
                    raise ValueError(
                        f"repeated_runs_passed evidence must state at least {MIN_REPEATED_RUNS} runs, e.g. '5 consecutive runs'"
                    )
        return self


class DraftOrigin(BaseModel):
    """Provenance of an automatically drafted incident; its diagnosis is a hypothesis until reviewed."""

    generated_by: Literal["circa investigate-log"] = "circa investigate-log"
    ai_generated: Literal[True] = True
    llm_model: str | None = None
    source_log: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    validation_steps: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    related_incident_ids: list[str] = Field(default_factory=list)
    # Where the draft's category came from: the analyzer alone, the analyzer via
    # historical matches, the LLM because deterministic analysis found none, or a
    # reviewer who confirmed or corrected it.
    category_source: Literal["deterministic", "historical", "llm", "reviewer"] | None = None
    cited_log_lines: list[str] = Field(default_factory=list)


class HistoricalIncident(BaseModel):
    """A known local incident used for deterministic comparison.

    ``synthetic`` records provenance. ``status`` records the review lifecycle;
    only ``verified`` incidents participate in matching. Its default keeps the
    packaged synthetic incidents valid; team knowledge files must set it.
    """

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: CauseCategory
    summary: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    recommendation: str = Field(min_length=1)
    synthetic: bool
    status: IncidentStatus = "verified"
    fingerprints: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_LINES)
    root_cause: str | None = None
    fix: IncidentFix | None = None
    superseded_by: str | None = None
    reviewed_by: str | None = None
    created: date | None = None
    updated: date | None = None
    draft_origin: DraftOrigin | None = None

    @model_validator(mode="after")
    def _check_lifecycle(self) -> Self:
        for line in self.evidence:
            if not line.strip() or "\n" in line or len(line) > MAX_EVIDENCE_CHARS:
                raise ValueError(
                    f"each evidence item must be one non-empty line of at most {MAX_EVIDENCE_CHARS} characters; "
                    "store short sanitized excerpts, never raw logs"
                )
        if (self.status == "superseded") != (self.superseded_by is not None):
            raise ValueError("superseded_by is required exactly when status is 'superseded'")
        if self.superseded_by == self.id:
            raise ValueError("an incident cannot supersede itself")
        if self.status == "verified" and not self.synthetic:
            fix = self.fix
            if not (self.root_cause or "").strip():
                raise ValueError("a verified real incident requires a root_cause")
            if fix is None or fix.validation_status != "validated":
                raise ValueError("a verified real incident requires a fix with validation_status 'validated'")
            if not (self.reviewed_by or "").strip():
                raise ValueError("a verified real incident requires reviewed_by")
        if any(contains_secret(text) for text in self._text_fields()):
            raise ValueError("incident text matches a secret or credential pattern; redact it before storing")
        return self

    def _text_fields(self) -> list[str]:
        texts = [self.id, self.title, self.summary, self.recommendation, *self.keywords, *self.evidence]
        texts += [*self.environment.keys(), *self.environment.values()]
        texts += [text for text in (self.root_cause, self.reviewed_by) if text]
        if self.fix:
            texts += [self.fix.description, *self.fix.references]
            texts += [self.fix.validation_evidence] if self.fix.validation_evidence else []
        if self.draft_origin:
            origin = self.draft_origin
            texts += [origin.source_log, *origin.validation_steps, *origin.missing_evidence]
            texts += [*origin.related_incident_ids, *([origin.llm_model] if origin.llm_model else [])]
        return texts


class SemanticIncidentMatch(BaseModel):
    """A historical incident ranked by local embedding similarity."""

    incident: HistoricalIncident
    similarity: float = Field(ge=-1.0, le=1.0)


class KeywordIncidentMatch(BaseModel):
    """A historical incident ranked by the deterministic keyword score."""

    incident: HistoricalIncident
    score: float = Field(gt=0.0)


class HistoricalIncidentCandidate(BaseModel):
    """A deduplicated historical candidate with explicit retrieval evidence."""

    incident: HistoricalIncident
    keyword_score: float | None = Field(default=None, gt=0.0)
    semantic_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)
    keyword_rank: int | None = Field(default=None, ge=1)
    semantic_rank: int | None = Field(default=None, ge=1)
    fusion_score: float = Field(ge=0.0)
    sources: list[Literal["keyword", "semantic"]] = Field(default_factory=list)


class LLMClassification(BaseModel):
    """An independent, structured classification supplied by a local LLM."""

    category: CauseCategory
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class LLMDiagnosis(LLMClassification):
    """A structured local-LLM diagnosis hypothesis; it never verifies a fix."""

    suggested_fix: str | None = None
    validation_steps: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class PerformanceMeasurement(BaseModel):
    """A performance value extracted only from an explicit log comparison."""

    metric: str = Field(min_length=1)
    actual_value: float
    expected_range: str = Field(min_length=1)
    unit: str | None = None
    tolerance: str | None = None


class PerformanceAssessment(BaseModel):
    """A deterministic, non-authoritative assessment of a performance failure."""

    failure_type: Literal["performance_threshold_violation"]
    candidate_cause: Literal[
        "hardware_firmware_performance_drift", "possible_genuine_performance_regression", "undetermined"
    ]
    recommended_action: Literal["performance_threshold_recalibration"] | None = None
    measurements: list[PerformanceMeasurement] = Field(default_factory=list)
    test_details: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    human_validation_required: Literal[True] = True
    recommendation: str = Field(min_length=1)


class AnalysisResult(BaseModel):
    """The structured result produced by deterministic local analysis."""

    fingerprint: str = Field(min_length=1)
    category: CauseCategory
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    root_cause: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    recommendation: str = Field(min_length=1)
    owner: str = Field(default="unknown", min_length=1)
    matched_incident_ids: list[str] = Field(default_factory=list)
    llm_explanation: str | None = None
    llm_classification: LLMClassification | None = None
    final_category: CauseCategory | None = None
    final_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    final_root_cause: str | None = None
    performance_assessment: PerformanceAssessment | None = None
