"""Explainable combined retrieval over local historical incidents."""

from __future__ import annotations

from collections.abc import Iterable

from .knowledge import find_similar_incident_matches
from .models import CIFailure, HistoricalIncident, HistoricalIncidentCandidate
from .semantic import SemanticRetriever

_RRF_CONSTANT = 60


def retrieve_historical_candidates(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident],
    *,
    semantic_retriever: SemanticRetriever | None = None,
    keyword_limit: int = 3,
    semantic_limit: int = 3,
    limit: int = 3,
) -> list[HistoricalIncidentCandidate]:
    """Return stable, deduplicated candidates from keyword and semantic retrieval.

    Keyword score and cosine similarity remain separate because they have
    different meanings. Ranking uses reciprocal-rank fusion (RRF), which only
    combines each method's ordinal rank: ``sum(1 / (60 + rank))``.
    """

    if limit < 1:
        return []
    available_incidents = list(incidents)
    keyword_matches = find_similar_incident_matches(failure, available_incidents, keyword_limit)
    semantic_matches = (
        semantic_retriever.retrieve(failure, available_incidents, semantic_limit)
        if semantic_retriever is not None
        else []
    )
    # ``None`` means the optional embedding backend was unavailable.
    semantic_matches = semantic_matches or []

    candidates: dict[str, HistoricalIncidentCandidate] = {}
    for rank, match in enumerate(keyword_matches, start=1):
        candidates[match.incident.id] = HistoricalIncidentCandidate(
            incident=match.incident,
            keyword_score=match.score,
            keyword_rank=rank,
            fusion_score=1 / (_RRF_CONSTANT + rank),
            sources=["keyword"],
        )
    for rank, semantic_match in enumerate(semantic_matches, start=1):
        existing = candidates.get(semantic_match.incident.id)
        semantic_score = 1 / (_RRF_CONSTANT + rank)
        if existing is None:
            candidates[semantic_match.incident.id] = HistoricalIncidentCandidate(
                incident=semantic_match.incident,
                semantic_similarity=semantic_match.similarity,
                semantic_rank=rank,
                fusion_score=semantic_score,
                sources=["semantic"],
            )
            continue
        candidates[semantic_match.incident.id] = existing.model_copy(
            update={
                "semantic_similarity": semantic_match.similarity,
                "semantic_rank": rank,
                "fusion_score": existing.fusion_score + semantic_score,
                "sources": [*existing.sources, "semantic"],
            }
        )

    ranked = list(candidates.values())
    ranked.sort(key=lambda candidate: (-candidate.fusion_score, candidate.incident.id))
    return ranked[:limit]
