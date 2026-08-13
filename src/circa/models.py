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


class AnalysisResult(BaseModel):
    """The structured result produced by a future analysis layer."""

    fingerprint: str = Field(min_length=1)
    category: CauseCategory
    root_cause: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    matched_incident_ids: list[str] = Field(default_factory=list)
