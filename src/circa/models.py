"""Core data models for local CIRCA analysis."""

from typing import Literal

from pydantic import BaseModel, Field


CauseCategory = Literal["hardware", "software", "network", "infrastructure", "unknown"]


class CIFailure(BaseModel):
    """A CI failure and the local context available for matching."""

    failure_text: str = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    category: CauseCategory | None = None


class HistoricalIncident(BaseModel):
    """A known local incident used for deterministic comparison."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: CauseCategory
    summary: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    recommendation: str = Field(min_length=1)
    synthetic: Literal[True]


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
