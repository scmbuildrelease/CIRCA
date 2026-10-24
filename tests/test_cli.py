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


DRAFT = Path(__file__).parents[1] / "examples" / "incident_draft.json"
DISK_LOG = Path(__file__).parents[1] / "examples" / "demo_disk_full.log"
EXAMPLE_ID = "team-hardware-scratch-disk-001"


def _verified_knowledge(tmp_path: Path) -> Path:
    path = tmp_path / "team.json"
    assert runner.invoke(app, ["knowledge", "add", str(DRAFT), "--knowledge", str(path), "--yes"]).exit_code == 0
    verify = ["knowledge", "verify", EXAMPLE_ID, "--knowledge", str(path), "--reviewer", "reviewer", "--yes"]
    assert runner.invoke(app, verify).exit_code == 0
    return path


def test_knowledge_add_declined_confirmation_writes_nothing(tmp_path) -> None:
    path = tmp_path / "team.json"

    result = runner.invoke(app, ["knowledge", "add", str(DRAFT), "--knowledge", str(path)], input="n\n")

    assert result.exit_code == 1
    assert "Aborted; nothing was written." in result.output
    assert not path.exists()


def test_knowledge_add_accepted_confirmation_writes_pending_incident(tmp_path) -> None:
    path = tmp_path / "team.json"

    result = runner.invoke(app, ["knowledge", "add", str(DRAFT), "--knowledge", str(path)], input="y\n")

    assert result.exit_code == 0
    assert json.loads(path.read_text())[0]["status"] == "pending_review"


def test_knowledge_commands_exit_nonzero_on_errors(tmp_path) -> None:
    path = tmp_path / "team.json"
    add = ["knowledge", "add", str(DRAFT), "--knowledge", str(path), "--yes"]
    assert runner.invoke(app, add).exit_code == 0

    duplicate = runner.invoke(app, add)
    missing = runner.invoke(app, ["knowledge", "verify", "missing", "--knowledge", str(path), "--reviewer", "r"])
    bad_draft = tmp_path / "bad.json"
    bad_draft.write_text("[1, 2]")
    malformed = runner.invoke(app, ["knowledge", "add", str(bad_draft), "--knowledge", str(path), "--yes"])

    assert duplicate.exit_code == 1 and "already exists" in duplicate.output
    assert missing.exit_code == 1 and "not found" in missing.output
    assert malformed.exit_code == 1 and "one JSON incident object" in malformed.output


def test_knowledge_validate_and_list_report_status(tmp_path) -> None:
    path = _verified_knowledge(tmp_path)

    validated = runner.invoke(app, ["knowledge", "validate", str(path)])
    listed = runner.invoke(app, ["knowledge", "list", "--knowledge", str(path)])
    path.write_text('[{"id": "x"}]')
    invalid = runner.invoke(app, ["knowledge", "validate", str(path)])

    assert validated.exit_code == 0 and "1 verified" in validated.output
    assert f"{EXAMPLE_ID}\tverified\tsynthetic" in listed.output
    assert invalid.exit_code == 1 and "explicit 'status'" in invalid.output


def test_knowledge_supersede_command(tmp_path) -> None:
    path = _verified_knowledge(tmp_path)
    newer = json.loads(DRAFT.read_text()) | {"id": "team-newer-001", "evidence": ["cleanup daemon crashed"]}
    newer_draft = tmp_path / "newer.json"
    newer_draft.write_text(json.dumps(newer))
    add = ["knowledge", "add", str(newer_draft), "--knowledge", str(path), "--yes", "--allow-similar"]
    assert runner.invoke(app, add).exit_code == 0
    verify = ["knowledge", "verify", "team-newer-001", "--knowledge", str(path), "--reviewer", "r", "--yes"]
    assert runner.invoke(app, verify).exit_code == 0

    result = runner.invoke(
        app, ["knowledge", "supersede", EXAMPLE_ID, "--by", "team-newer-001", "--knowledge", str(path), "--yes"]
    )

    assert result.exit_code == 0
    assert {record["id"]: record["status"] for record in json.loads(path.read_text())}[EXAMPLE_ID] == "superseded"


def test_analysis_with_knowledge_matches_verified_incident_and_never_writes(tmp_path) -> None:
    path = _verified_knowledge(tmp_path)
    before = path.read_bytes()

    log_result = runner.invoke(app, ["analyze-log", str(DISK_LOG), "--knowledge", str(path), "--json"])
    json_result = runner.invoke(
        app,
        ["analyze", str(Path(__file__).parents[1] / "examples" / "hardware_failure.json"), "--knowledge", str(path)],
    )

    assert log_result.exit_code == 0 and json_result.exit_code == 0
    assert json.loads(log_result.output)["matched_incident_ids"][0] == EXAMPLE_ID
    assert path.read_bytes() == before
    assert [item.name for item in tmp_path.iterdir()] == ["team.json"]


def test_analysis_with_pending_knowledge_is_identical_to_without(tmp_path) -> None:
    path = tmp_path / "team.json"
    assert runner.invoke(app, ["knowledge", "add", str(DRAFT), "--knowledge", str(path), "--yes"]).exit_code == 0

    without = runner.invoke(app, ["analyze-log", str(DISK_LOG), "--json"])
    pending = runner.invoke(app, ["analyze-log", str(DISK_LOG), "--knowledge", str(path), "--json"])

    assert without.exit_code == pending.exit_code == 0
    assert without.output == pending.output


def test_analysis_with_invalid_knowledge_file_exits_nonzero(tmp_path) -> None:
    path = tmp_path / "team.json"
    path.write_text('[{"id": "x"}]')

    result = runner.invoke(app, ["analyze-log", str(DISK_LOG), "--knowledge", str(path)])

    assert result.exit_code == 1
    assert "explicit 'status'" in result.output
