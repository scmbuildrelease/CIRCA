import json
from pathlib import Path

import pytest

from circa.benchmark import EvaluationCase, calculate_retrieval_metrics, load_evaluation_cases, run_retrieval_benchmark
from circa.models import HistoricalIncident
from circa.semantic import SemanticIncidentRetriever


class StaticEmbeddingProvider:
    def __init__(self, embeddings: list[list[float]] | None) -> None:
        self.embeddings = embeddings

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return self.embeddings


def _incident(identifier: str, keywords: list[str]) -> HistoricalIncident:
    return HistoricalIncident(
        id=identifier, title=identifier, category="hardware", summary=identifier,
        keywords=keywords, recommendation="Investigate.", synthetic=True,
    )


def _case(identifier: str, relevant: list[str]) -> EvaluationCase:
    return EvaluationCase(
        id=identifier, failure_text="disk full", expected_category="hardware", relevant_incident_ids=relevant
    )


def test_metrics_use_only_known_relevance_cases_for_ranking_denominators() -> None:
    metrics = calculate_retrieval_metrics(
        [_case("one", ["a"]), _case("two", ["b"]), _case("ambiguous", [])],
        [["a"], ["x", "b"], []],
        3,
    )

    assert metrics.top_1_accuracy == 0.5
    assert metrics.recall_at_k == 1.0
    assert metrics.mean_reciprocal_rank == 0.75
    assert metrics.no_result_rate == pytest.approx(1 / 3)
    assert metrics.evaluation_coverage == 1.0
    assert metrics.no_known_relevance_cases == 1


def test_metrics_mark_undefined_ranking_metrics_unavailable() -> None:
    metrics = calculate_retrieval_metrics([_case("ambiguous", [])], [[]], 3)

    assert metrics.top_1_accuracy is None
    assert metrics.recall_at_k is None
    assert metrics.mean_reciprocal_rank is None


def test_benchmark_compares_existing_keyword_semantic_and_combined_retrieval() -> None:
    cases = [_case("one", ["disk"])]
    incidents = [_incident("disk", ["disk", "full"])]
    retriever = SemanticIncidentRetriever(StaticEmbeddingProvider([[1, 0], [1, 0]]), threshold=0.1)

    report = run_retrieval_benchmark(cases, incidents, semantic_retriever=retriever)

    assert [method.name for method in report.methods] == ["keyword", "semantic", "combined"]
    assert all(method.metrics.recall_at_k == 1.0 for method in report.methods)
    assert report.methods[1].status == "available"
    assert report.methods[2].status == "combined"


def test_benchmark_marks_semantic_unavailable_and_combined_fallback() -> None:
    cases = [_case("one", ["disk"])]
    incidents = [_incident("disk", ["disk", "full"])]
    retriever = SemanticIncidentRetriever(StaticEmbeddingProvider(None))

    report = run_retrieval_benchmark(cases, incidents, semantic_retriever=retriever)

    semantic, combined = report.methods[1:]
    assert semantic.status == "unavailable"
    assert semantic.metrics.evaluated_cases == 0
    assert semantic.metrics.recall_at_k is None
    assert combined.status == "keyword fallback for 1 case(s)"
    assert combined.metrics.recall_at_k == 1.0


def test_evaluation_dataset_is_synthetic_and_documented() -> None:
    path = Path(__file__).parents[1] / "data" / "retrieval_evaluation.json"
    payload = json.loads(path.read_text())
    cases = load_evaluation_cases(path)

    assert "labeling_conventions" in payload
    assert 30 <= len(cases) <= 50
    assert all(case.synthetic for case in cases)
