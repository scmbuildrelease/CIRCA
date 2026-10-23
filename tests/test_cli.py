import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from circa.cli import app
from circa.knowledge import load_incidents
from circa.models import LLMClassification, SemanticIncidentMatch
from circa.resources import data_path

runner = CliRunner()


def test_analyze_prints_human_readable_report() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"

    result = runner.invoke(app, ["analyze", str(example)])

    assert result.exit_code == 0
    assert "Category: hardware" in result.output
    assert "Confidence:" in result.output
    assert "Historical matches:" in result.output
    assert "Owner: hardware-ops" in result.output
    assert "Fingerprint:" in result.output
    assert "LLM explanation:" not in result.output


def test_analyze_json_prints_analysis_result() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"

    result = runner.invoke(app, ["analyze", str(example), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["category"] == "hardware"
    assert payload["owner"] == "hardware-ops"
    assert payload["matched_incident_ids"][0] == "synthetic-hardware-disk-001"


def test_analyze_log_runs_the_local_pipeline() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.log"

    result = runner.invoke(app, ["analyze-log", str(example), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["category"] == "hardware"
    assert payload["owner"] == "hardware-ops"


def test_analyze_llm_adds_optional_explanation() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    with patch("circa.cli.explain_analysis") as explain:
        explain.side_effect = lambda _, result: result.model_copy(update={"llm_explanation": "Local explanation."})
        result = runner.invoke(app, ["analyze", str(example), "--llm", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["llm_explanation"] == "Local explanation."
    explain.assert_called_once()


def test_analyze_llm_classify_prints_independent_classification() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    classification = LLMClassification(
        category="hardware", confidence=0.92, root_cause="Disk is full.", evidence=["no space left"]
    )
    with patch("circa.cli.classify_failure", return_value=classification) as classify:
        result = runner.invoke(app, ["analyze", str(example), "--llm-classify"])

    assert result.exit_code == 0
    assert "Deterministic Analysis" in result.output
    assert "LLM Classification" in result.output
    assert "Final Hybrid Analysis" in result.output
    assert "Confidence: 0.92" in result.output
    classify.assert_called_once()


def test_analyze_llm_classify_includes_structured_json() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    classification = LLMClassification(
        category="hardware", confidence=0.92, root_cause="Disk is full.", evidence=["no space left"]
    )
    with patch("circa.cli.classify_failure", return_value=classification):
        result = runner.invoke(app, ["analyze", str(example), "--llm-classify", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["deterministic_analysis"]["category"] == "hardware"
    assert payload["llm_classification"]["confidence"] == 0.92
    assert payload["final_hybrid_analysis"]["category"] == "hardware"
    assert payload["final_hybrid_analysis"]["root_cause"] == (
        "Build failed because the worker disk had no space left for compiler output."
    )


def test_analyze_without_llm_classify_does_not_call_classifier() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    with patch("circa.cli.classify_failure") as classify:
        result = runner.invoke(app, ["analyze", str(example)])

    assert result.exit_code == 0
    classify.assert_not_called()


def test_analyze_semantic_retrieve_prints_ranked_matches() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    semantic_match = SemanticIncidentMatch(
        incident=load_incidents(data_path("incidents.json"))[0],
        similarity=0.91,
    )
    with patch("circa.cli.SemanticIncidentRetriever.retrieve", return_value=[semantic_match]):
        result = runner.invoke(app, ["analyze", str(example), "--semantic-retrieve"])

    assert result.exit_code == 0
    assert "Semantic Historical Matches" in result.output
    assert "0.91" in result.output


def test_analyze_semantic_retrieve_includes_json_matches() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    semantic_match = SemanticIncidentMatch(
        incident=load_incidents(data_path("incidents.json"))[0],
        similarity=0.91,
    )
    with patch("circa.cli.SemanticIncidentRetriever.retrieve", return_value=[semantic_match]):
        result = runner.invoke(app, ["analyze", str(example), "--semantic-retrieve", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["semantic_matches"][0]["similarity"] == 0.91


def test_unavailable_semantic_retrieval_preserves_keyword_analysis() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    with patch("circa.cli.SemanticIncidentRetriever.retrieve", return_value=None):
        result = runner.invoke(app, ["analyze", str(example), "--semantic-retrieve"])

    assert result.exit_code == 0
    assert "Warning: semantic retrieval unavailable." in result.output
    assert "Category: hardware" in result.output


def test_unavailable_semantic_retrieval_is_null_in_json() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    with patch("circa.cli.SemanticIncidentRetriever.retrieve", return_value=None):
        result = runner.invoke(app, ["analyze", str(example), "--semantic-retrieve", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["semantic_matches"] is None


def test_benchmark_prints_retrieval_metrics_with_semantic_availability() -> None:
    with patch("circa.cli.run_retrieval_benchmark") as run:
        from circa.benchmark import BenchmarkMethodResult, BenchmarkReport, RetrievalMetrics

        metrics = RetrievalMetrics(
            evaluated_cases=1,
            skipped_cases=0,
            relevant_cases=1,
            no_known_relevance_cases=0,
            top_1_accuracy=1.0,
            recall_at_k=1.0,
            mean_reciprocal_rank=1.0,
            no_result_rate=0.0,
            evaluation_coverage=1.0,
        )
        run.return_value = BenchmarkReport(
            dataset_size=1,
            top_k=3,
            methods=[
                BenchmarkMethodResult(name="keyword", status="available", metrics=metrics),
                BenchmarkMethodResult(name="semantic", status="unavailable", metrics=metrics),
                BenchmarkMethodResult(name="combined", status="keyword fallback for 1 case(s)", metrics=metrics),
            ],
        )
        result = runner.invoke(app, ["benchmark"])

    assert result.exit_code == 0
    assert "CIRCA Retrieval Benchmark: 1 dataset cases; Top-K: 3" in result.output
    assert "Semantic retrieval (unavailable)" in result.output
    assert "Recall@3 (relevant-label cases): 1.000" in result.output
