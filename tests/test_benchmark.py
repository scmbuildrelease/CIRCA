import json

import pytest

from circa.benchmark import EvaluationCase, calculate_retrieval_metrics, load_evaluation_cases, run_retrieval_benchmark
from circa.knowledge import load_incidents
from circa.models import HistoricalIncident
from circa.resources import data_path
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
        summary=identifier,
        keywords=keywords,
        recommendation="Investigate.",
        synthetic=True,
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


def test_empty_evaluation_dataset_has_undefined_metrics() -> None:
    metrics = calculate_retrieval_metrics([], [], 3)

    assert metrics.evaluated_cases == 0
    assert metrics.skipped_cases == 0
    assert metrics.top_1_accuracy is None
    assert metrics.recall_at_k is None
    assert metrics.mean_reciprocal_rank is None
    assert metrics.no_result_rate is None
    assert metrics.evaluation_coverage is None


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
    path = data_path("retrieval_evaluation.json")
    payload = json.loads(path.read_text())
    cases = load_evaluation_cases(path)

    assert "labeling_conventions" in payload
    assert 30 <= len(cases) <= 50
    assert all(case.synthetic for case in cases)


def test_packaged_data_runs_keyword_benchmark_without_semantic_backend() -> None:
    cases = load_evaluation_cases(data_path("retrieval_evaluation.json"))
    report = run_retrieval_benchmark(cases, load_incidents(data_path("incidents.json")), semantic_retriever=None)

    assert report.dataset_size == 35
    assert report.methods[0].metrics.evaluated_cases == 35
    assert report.methods[1].status == "unavailable"
    assert report.methods[2].status == "keyword fallback for 35 case(s)"


def test_dataset_validation_rejects_duplicate_case_ids(tmp_path) -> None:
    dataset = tmp_path / "duplicate.json"
    dataset.write_text(json.dumps({"cases": [_case("same", []).model_dump(), _case("same", []).model_dump()]}))

    with pytest.raises(ValueError, match="Duplicate evaluation case ID 'same'"):
        load_evaluation_cases(dataset, [])


def test_dataset_validation_rejects_unknown_incident_reference(tmp_path) -> None:
    dataset = tmp_path / "unknown-reference.json"
    record = _case("unknown-reference", ["missing-incident"]).model_dump()
    dataset.write_text(json.dumps({"cases": [record]}))

    with pytest.raises(ValueError, match=r"unknown historical incident ID\(s\): missing-incident"):
        load_evaluation_cases(dataset, [_incident("known", [])])


def test_dataset_validation_accepts_empty_relevance_and_reports_malformed_records(tmp_path) -> None:
    valid = tmp_path / "ambiguous.json"
    valid.write_text(json.dumps({"cases": [_case("ambiguous", []).model_dump()]}))
    assert load_evaluation_cases(valid, [])[0].relevant_incident_ids == []

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"cases": [{"id": "bad", "failure_text": "x", "expected_category": "invalid"}]}))
    with pytest.raises(ValueError, match="Invalid evaluation case at index 0"):
        load_evaluation_cases(malformed, [])

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{")
    with pytest.raises(ValueError, match="Invalid evaluation dataset JSON"):
        load_evaluation_cases(invalid_json, [])


def test_benchmark_queries_semantic_backend_once_per_case() -> None:
    class CountingProvider(StaticEmbeddingProvider):
        calls = 0

        def embed(self, texts: list[str]) -> list[list[float]] | None:
            CountingProvider.calls += 1
            return [[1.0, 0.0] for _ in texts]

    incidents = [_incident("a", ["disk"])]
    report = run_retrieval_benchmark(
        [_case("one", ["a"]), _case("two", ["a"])],
        incidents,
        semantic_retriever=SemanticIncidentRetriever(CountingProvider(None)),
    )

    assert CountingProvider.calls == 2
    assert report.methods[2].status == "combined"
