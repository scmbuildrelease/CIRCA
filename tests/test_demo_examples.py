import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from typer.testing import CliRunner

from circa.cli import app
from circa.pipeline import analyze_log

EXAMPLES = Path(__file__).parents[1] / "examples"
runner = CliRunner()


def test_documented_disk_full_demo_has_hardware_evidence() -> None:
    raw_log = (Path(__file__).parents[1] / "examples" / "demo_disk_full.log").read_text()

    result = analyze_log(raw_log)

    assert result.category == "hardware"
    assert result.owner == "hardware-ops"
    assert "synthetic-hardware-disk-001" in result.matched_incident_ids
    assert result.evidence


def test_documented_insufficient_evidence_demo_stays_unknown() -> None:
    raw_log = (Path(__file__).parents[1] / "examples" / "demo_insufficient_evidence.log").read_text()

    result = analyze_log(raw_log)

    assert result.category == "unknown"
    assert result.confidence == 0.0
    assert result.matched_incident_ids == []


# The three demos in docs/INTERVIEW_DEMO.md, run with the documented commands against a temporary knowledge file.
def _circa(*args: str) -> dict:
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _no_ollama():
    return patch("circa.llm.urlopen", side_effect=URLError(ConnectionRefusedError(61, "Connection refused")))


def test_demo_a_known_failure_is_answered_from_verified_history_without_the_llm(tmp_path: Path) -> None:
    knowledge = str(tmp_path / "demo-team.json")
    for command in (
        ["knowledge", "add", str(EXAMPLES / "incident_draft.json")],
        ["knowledge", "verify", "team-hardware-scratch-disk-001", "--reviewer", "Demo Reviewer"],
    ):
        assert runner.invoke(app, [*command, "--knowledge", knowledge, "--yes"]).exit_code == 0

    with patch("circa.llm.urlopen") as ollama:
        report = _circa("investigate-log", str(EXAMPLES / "demo_disk_full.log"), "--knowledge", knowledge)

    ollama.assert_not_called()
    assert report["outcome"] == "known_solution" and report["llm_called"] is False
    assert report["matched_incident"]["id"] == "team-hardware-scratch-disk-001"


def test_demo_b_unknown_failure_drafts_a_pending_review_hypothesis_with_ollama(tmp_path: Path) -> None:
    knowledge = tmp_path / "demo-team.json"
    diagnosis = {
        "category": "infrastructure",
        "confidence": 0.7,
        "root_cause": "The signing certificate chain expired, so signtool rejected the bundle.",
        "evidence": ["2026-10-24T09:30:07Z [ERROR] signtool rejected bundle: certificate chain expired on 2026-10-23"],
        "suggested_fix": "Renew the signing certificate chain.",
        "validation_steps": [],
        "missing_evidence": [],
    }
    response = MagicMock()
    response.read.return_value = json.dumps({"response": json.dumps(diagnosis)}).encode()
    response.__enter__.return_value = response

    with patch("circa.llm.urlopen", return_value=response):
        report = _circa(
            "investigate-log", str(EXAMPLES / "demo_unknown_failure.log"), "--knowledge", str(knowledge), "--synthetic"
        )

    assert report["outcome"] == "new_diagnosis" and report["llm_called"] is True
    [draft] = json.loads(knowledge.read_text())
    assert draft["status"] == "pending_review"
    assert draft["fix"]["validation_status"] == "unvalidated"  # an AI diagnosis is never verified on its own


def test_demo_b_without_ollama_falls_back_to_deterministic_analysis(tmp_path: Path) -> None:
    knowledge = tmp_path / "demo-team.json"

    with _no_ollama():
        report = _circa("investigate-log", str(EXAMPLES / "demo_unknown_failure.log"), "--knowledge", str(knowledge))

    assert report["outcome"] == "deterministic_only" and report["llm_called"] is False
    assert report["analysis"]["category"] == "unknown"
    assert not knowledge.exists()


def test_demo_c_insufficient_evidence_is_unknown_and_never_asks_the_llm(tmp_path: Path) -> None:
    with patch("circa.llm.urlopen") as ollama:
        report = _circa(
            "investigate-log", str(EXAMPLES / "demo_insufficient_evidence.log"), "--knowledge", str(tmp_path / "k.json")
        )

    ollama.assert_not_called()
    assert report["outcome"] == "no_failure_evidence" and report["llm_called"] is False
    assert report["analysis"]["category"] == "unknown"
    assert report["analysis"]["root_cause"] == "Insufficient deterministic evidence to identify a root cause."
