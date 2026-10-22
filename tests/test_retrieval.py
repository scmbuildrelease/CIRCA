from circa.analyzer import analyze_failure
from circa.models import CIFailure, HistoricalIncident
from circa.retrieval import retrieve_historical_candidates
from circa.semantic import SemanticIncidentRetriever


class StaticEmbeddingProvider:
    def __init__(self, embeddings: list[list[float]] | None) -> None:
        self.embeddings = embeddings

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return self.embeddings


def _incident(identifier: str, keywords: list[str]) -> HistoricalIncident:
    return HistoricalIncident(
        id=identifier,
        title=identifier,
        category="hardware",
        summary=f"{identifier} summary",
        keywords=keywords,
        recommendation="Investigate it.",
        synthetic=True,
    )


def test_keyword_only_retrieval_retains_keyword_score_and_rank() -> None:
    failure = CIFailure(failure_text="disk full")
    incidents = [_incident("disk", ["disk", "full"]), _incident("other", ["network"])]

    candidates = retrieve_historical_candidates(failure, incidents)

    assert [item.incident.id for item in candidates] == ["disk"]
    assert candidates[0].sources == ["keyword"]
    assert candidates[0].keyword_score == 2.0
    assert candidates[0].keyword_rank == 1
    assert candidates[0].semantic_similarity is None


def test_semantic_only_retrieval_keeps_cosine_similarity_separate() -> None:
    failure = CIFailure(failure_text="unrelated wording")
    incidents = [_incident("semantic", ["disk"])]
    retriever = SemanticIncidentRetriever(StaticEmbeddingProvider([[1, 0], [1, 0]]), threshold=0.1)

    candidates = retrieve_historical_candidates(failure, incidents, semantic_retriever=retriever, keyword_limit=0)

    assert [item.incident.id for item in candidates] == ["semantic"]
    assert candidates[0].sources == ["semantic"]
    assert candidates[0].keyword_score is None
    assert candidates[0].semantic_similarity == 1.0


def test_combined_ranking_uses_rank_fusion_and_deduplicates_matches() -> None:
    failure = CIFailure(failure_text="disk full")
    incidents = [
        _incident("keyword-best", ["disk", "full", "storage"]),
        _incident("shared", ["disk", "full"]),
        _incident("semantic-best", ["unrelated"]),
    ]
    retriever = SemanticIncidentRetriever(
        StaticEmbeddingProvider([[1, 0], [0.1, 0.9], [0.9, 0.1], [1, 0]]), threshold=0.5
    )

    candidates = retrieve_historical_candidates(failure, incidents, semantic_retriever=retriever)

    assert [item.incident.id for item in candidates] == ["shared", "keyword-best", "semantic-best"]
    shared = candidates[0]
    assert shared.sources == ["keyword", "semantic"]
    assert shared.keyword_rank == 2
    assert shared.semantic_rank == 2
    assert shared.fusion_score == (1 / 62) + (1 / 62)
    assert len({item.incident.id for item in candidates}) == len(candidates)


def test_unavailable_semantic_backend_falls_back_to_keyword_results() -> None:
    failure = CIFailure(failure_text="disk full")
    incidents = [_incident("disk", ["disk", "full"])]
    unavailable = SemanticIncidentRetriever(StaticEmbeddingProvider(None))

    candidates = retrieve_historical_candidates(failure, incidents, semantic_retriever=unavailable)

    assert [item.incident.id for item in candidates] == ["disk"]
    assert candidates[0].sources == ["keyword"]


def test_empty_results_and_stable_tiebreaking() -> None:
    failure = CIFailure(failure_text="nothing related")
    assert retrieve_historical_candidates(failure, []) == []

    incidents = [_incident("b", ["disk"]), _incident("a", ["disk"])]
    first = retrieve_historical_candidates(CIFailure(failure_text="disk"), incidents)
    second = retrieve_historical_candidates(CIFailure(failure_text="disk"), incidents)
    assert [item.incident.id for item in first] == ["a", "b"]
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]


def test_combined_retrieval_does_not_change_analyzer_behavior() -> None:
    failure = CIFailure(failure_text="disk full")
    incidents = [_incident("disk", ["disk", "full"])]

    result = analyze_failure(failure, incidents)

    assert result.category == "hardware"
    assert result.matched_incident_ids == ["disk"]
