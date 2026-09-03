import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from circa.cli import app
from circa.models import LLMClassification

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


def test_analyze_without_llm_classify_does_not_call_classifier() -> None:
    example = Path(__file__).parents[1] / "examples" / "hardware_failure.json"
    with patch("circa.cli.classify_failure") as classify:
        result = runner.invoke(app, ["analyze", str(example)])

    assert result.exit_code == 0
    classify.assert_not_called()
