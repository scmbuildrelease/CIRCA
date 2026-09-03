import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from circa.cli import app

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
