import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from circa.cli import app
from circa.models import HistoricalIncident, LLMDiagnosis
from circa.pipeline import analyze_log, investigate_log

runner = CliRunner()
EXAMPLES = Path(__file__).parents[1] / "examples"
DISK_LOG = EXAMPLES / "demo_disk_full.log"
UNKNOWN_LOG = EXAMPLES / "demo_unknown_failure.log"
DRAFT = EXAMPLES / "incident_draft.json"
SIGNTOOL_LINE = "2026-10-24T09:30:07Z [ERROR] signtool rejected bundle: certificate chain expired on 2026-10-23"


class FakeProvider:
    """A mocked local LLM; tests never contact Ollama."""

    model = "fake-local"

    def __init__(self, diagnosis: LLMDiagnosis | None = None, **_: object) -> None:
        self.diagnosis = diagnosis
        self.prompts: list[str] = []

    def diagnose(self, prompt: str) -> LLMDiagnosis | None:
        self.prompts.append(prompt)
        return self.diagnosis


def _diagnosis(**overrides: object) -> LLMDiagnosis:
    fields = {
        "category": "infrastructure",
        "confidence": 0.6,
        "root_cause": "Hypothesis: the code-signing certificate chain expired.",
        "evidence": [SIGNTOOL_LINE],
        "suggested_fix": "Renew the signing certificate chain.",
        "validation_steps": ["Re-run packaging and confirm the bundle is signed."],
        "missing_evidence": ["Certificate expiry details from the signing service."],
    }
    return LLMDiagnosis.model_validate(fields | overrides)


def _invoke(provider: FakeProvider, *args: str):
    with patch("circa.cli.OllamaProvider", return_value=provider):
        return runner.invoke(app, ["investigate-log", *args])


def _verified_example(tmp_path: Path) -> Path:
    path = tmp_path / "team.json"
    assert runner.invoke(app, ["knowledge", "add", str(DRAFT), "--knowledge", str(path), "--yes"]).exit_code == 0
    verify = ["knowledge", "verify", "team-hardware-scratch-disk-001", "--knowledge", str(path)]
    assert runner.invoke(app, [*verify, "--reviewer", "reviewer", "--yes"]).exit_code == 0
    return path


def _drafted(tmp_path: Path, *extra: str) -> tuple[Path, str]:
    path = tmp_path / "team.json"
    result = _invoke(FakeProvider(_diagnosis()), str(UNKNOWN_LOG), "--knowledge", str(path), *extra)
    assert result.exit_code == 0, result.output
    return path, json.loads(path.read_text())[0]["id"]


def test_strong_verified_match_returns_the_fix_without_calling_the_llm(tmp_path: Path) -> None:
    path = _verified_example(tmp_path)
    provider = FakeProvider(_diagnosis())
    before = path.read_bytes()

    result = _invoke(provider, str(DISK_LOG), "--knowledge", str(path), "--json")

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert provider.prompts == []
    assert payload["outcome"] == "known_solution"
    assert payload["matched_incident"]["id"] == "team-hardware-scratch-disk-001"
    assert payload["matched_incident"]["fix"]["validation_status"] == "validated"
    assert path.read_bytes() == before


def test_unknown_failure_calls_the_llm_once_and_saves_a_pending_draft(tmp_path: Path) -> None:
    path = tmp_path / "team.json"
    provider = FakeProvider(_diagnosis(evidence=[SIGNTOOL_LINE, "an invented line"]))

    result = _invoke(provider, str(UNKNOWN_LOG), "--knowledge", str(path))

    assert result.exit_code == 0, result.output
    assert len(provider.prompts) == 1
    assert "Starting firmware packaging stage" not in provider.prompts[0]  # only extracted lines are sent
    [draft] = json.loads(path.read_text())
    assert draft["status"] == "pending_review"
    assert draft["synthetic"] is False
    assert draft["fix"]["validation_status"] == "unvalidated"
    assert draft["draft_origin"]["ai_generated"] is True
    assert draft["draft_origin"]["llm_model"] == "fake-local"
    assert draft["evidence"][0] == SIGNTOOL_LINE
    assert "Ignored 1 LLM-cited line(s)" in result.output
    assert "Draft saved: draft-" in result.output


def test_weak_or_unvalidated_history_is_not_a_confirmed_solution() -> None:
    # Packaged incidents match the disk log but carry no validated fix.
    provider = FakeProvider(_diagnosis(category="hardware"))

    investigation = investigate_log(DISK_LOG.read_text(), [], provider, source_name="disk.log")

    assert investigation.outcome == "new_diagnosis"
    assert investigation.matched_incident is None
    assert investigation.analysis.matched_incident_ids  # still reported as supporting evidence
    assert "Weak historical matches (not confirmed)" in provider.prompts[0]


@pytest.mark.parametrize("diagnosis", [None, _diagnosis(category="unknown")], ids=["unavailable", "unknown-cause"])
def test_unusable_llm_result_preserves_deterministic_analysis_without_a_draft(diagnosis: LLMDiagnosis | None) -> None:
    raw_log = UNKNOWN_LOG.read_text()

    investigation = investigate_log(raw_log, [], FakeProvider(diagnosis), source_name="unknown.log")

    assert investigation.outcome == "deterministic_only"
    assert investigation.draft is None
    assert investigation.analysis == analyze_log(raw_log)
    assert any("unconfirmed" in note for note in investigation.notes)


def test_log_without_failure_lines_skips_the_llm() -> None:
    provider = FakeProvider(_diagnosis())

    investigation = investigate_log(
        (EXAMPLES / "demo_insufficient_evidence.log").read_text(), [], provider, source_name="x.log"
    )

    assert investigation.outcome == "no_failure_evidence"
    assert provider.prompts == []


def test_repeated_failure_reuses_the_pending_draft_without_another_llm_call(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path)
    before = path.read_bytes()
    # Same failure on a different run: only volatile values differ.
    rerun = tmp_path / "rerun.log"
    rerun.write_text(UNKNOWN_LOG.read_text().replace("2026-10-24T09:30", "2026-10-25T11:12"))
    provider = FakeProvider(_diagnosis())

    result = _invoke(provider, str(rerun), "--knowledge", str(path))

    assert result.exit_code == 0
    assert provider.prompts == []
    assert f"Already recorded as pending_review incident '{draft_id}'" in result.output
    assert path.read_bytes() == before


def test_draft_review_show_edit_reject_and_list(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path)
    knowledge = ["--knowledge", str(path)]

    listed = runner.invoke(app, ["knowledge", "list", *knowledge, "--pending"])
    shown = runner.invoke(app, ["knowledge", "show", draft_id, *knowledge])
    exported = runner.invoke(app, ["knowledge", "show", draft_id, *knowledge, "--json"])
    edited = json.loads(exported.output) | {"root_cause": "Signing certificate chain expired on 2026-10-23."}
    edited_path = tmp_path / "edited.json"
    edited_path.write_text(json.dumps(edited))
    updated = runner.invoke(app, ["knowledge", "update", str(edited_path), *knowledge, "--yes"])
    rejected = runner.invoke(app, ["knowledge", "reject", draft_id, *knowledge, "--reviewer", "r", "--yes"])
    relisted = runner.invoke(app, ["knowledge", "list", *knowledge, "--pending"])

    assert f"{draft_id}\tpending_review\treal" in listed.output
    assert "Origin: AI-generated hypothesis (fake-local)" in shown.output
    assert "Fix validation: unvalidated" in shown.output
    assert "status" not in json.loads(exported.output)
    assert updated.exit_code == 0 and rejected.exit_code == 0
    [record] = json.loads(path.read_text())
    assert record["status"] == "rejected"
    assert record["root_cause"] == edited["root_cause"]
    assert draft_id not in relisted.output


def test_approval_never_validates_a_fix_without_reviewer_evidence(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path)
    verify = ["knowledge", "verify", draft_id, "--knowledge", str(path), "--reviewer", "r", "--yes"]

    refused = runner.invoke(app, verify)
    assert refused.exit_code == 1
    assert "validation_status 'validated'" in refused.output
    assert json.loads(path.read_text())[0]["status"] == "pending_review"

    evidence = ["--validation-method", "repeated_runs_passed", "--validation-evidence", "5 consecutive runs passed"]
    approved = runner.invoke(app, [*verify, *evidence])

    assert approved.exit_code == 0, approved.output
    [record] = json.loads(path.read_text())
    assert record["status"] == "verified"
    assert record["fix"]["validation_evidence"] == "5 consecutive runs passed"


def test_synthetic_approval_keeps_the_fix_unvalidated_and_is_reused(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path, "--synthetic")
    verify = ["knowledge", "verify", draft_id, "--knowledge", str(path), "--reviewer", "r", "--yes"]
    assert runner.invoke(app, verify).exit_code == 0
    provider = FakeProvider(_diagnosis())

    result = _invoke(provider, str(UNKNOWN_LOG), "--knowledge", str(path), "--json")

    payload = json.loads(result.output)
    assert json.loads(path.read_text())[0]["fix"]["validation_status"] == "unvalidated"
    assert payload["outcome"] == "existing_record"
    assert payload["matched_incident"]["status"] == "verified"
    assert provider.prompts == []


def test_verified_records_cannot_be_overwritten_or_rejected(tmp_path: Path) -> None:
    path = _verified_example(tmp_path)
    before = path.read_bytes()
    knowledge = ["--knowledge", str(path)]

    update = runner.invoke(app, ["knowledge", "update", str(DRAFT), *knowledge, "--yes"])
    reject = runner.invoke(
        app, ["knowledge", "reject", "team-hardware-scratch-disk-001", *knowledge, "--reviewer", "r"]
    )

    assert update.exit_code == 1 and "only pending_review incidents can be updated" in update.output
    assert reject.exit_code == 1 and "only pending_review can be rejected" in reject.output
    assert path.read_bytes() == before


def test_failed_draft_write_reports_an_error_and_leaves_knowledge_intact(tmp_path: Path) -> None:
    path = _verified_example(tmp_path)
    before = path.read_bytes()

    with patch("circa.cli.save_knowledge", side_effect=OSError("disk full")):
        result = _invoke(FakeProvider(_diagnosis()), str(UNKNOWN_LOG), "--knowledge", str(path))

    assert result.exit_code == 1
    assert "draft not saved: disk full" in result.output
    assert path.read_bytes() == before


def test_concurrent_modification_is_refused(tmp_path: Path) -> None:
    path = _verified_example(tmp_path)
    original_add = __import__("circa.cli", fromlist=["add_incident"]).add_incident

    def add_then_modify(*args: object, **kwargs: object):
        added = original_add(*args, **kwargs)
        path.write_text(path.read_text() + "\n")  # another writer changes the file
        return added

    with patch("circa.cli.add_incident", side_effect=add_then_modify):
        result = _invoke(FakeProvider(_diagnosis()), str(UNKNOWN_LOG), "--knowledge", str(path))

    assert result.exit_code == 1
    assert "changed since it was read" in result.output
    assert len(json.loads(path.read_text())) == 1


def test_secrets_are_neither_sent_nor_persisted(tmp_path: Path) -> None:
    secret = "token=" + "abc123secretvalue"
    log = tmp_path / "secret.log"
    log.write_text(UNKNOWN_LOG.read_text() + f"2026-10-24T09:30:09Z [ERROR] upload failed with {secret}\n")
    path = tmp_path / "team.json"
    provider = FakeProvider(_diagnosis())

    saved = _invoke(provider, str(log), "--knowledge", str(path))
    leaky = _invoke(
        FakeProvider(_diagnosis(root_cause=f"The job used {secret}.")),
        str(log),
        "--knowledge",
        str(tmp_path / "b.json"),
    )

    assert saved.exit_code == 0
    assert secret not in provider.prompts[0]
    assert secret not in path.read_text()
    assert leaky.exit_code == 1 and "secret or credential pattern" in leaky.output
    assert not (tmp_path / "b.json").exists()


def test_without_knowledge_nothing_is_written_and_analysis_is_unchanged(tmp_path: Path) -> None:
    result = _invoke(FakeProvider(_diagnosis()), str(DISK_LOG), "--json")
    analyzed = runner.invoke(app, ["analyze-log", str(DISK_LOG), "--json"])

    assert result.exit_code == 0
    assert "Draft not saved: pass --knowledge PATH" in result.output
    expected = {key: value for key, value in json.loads(analyzed.output).items() if value is not None}
    assert json.loads(result.stdout)["analysis"] == expected
    assert list(tmp_path.iterdir()) == []


def test_draft_records_validate_as_historical_incidents() -> None:
    investigation = investigate_log(UNKNOWN_LOG.read_text(), [], FakeProvider(_diagnosis()), source_name="u.log")

    assert investigation.draft is not None
    record = investigation.draft | {"status": "pending_review"}
    assert HistoricalIncident.model_validate(record).draft_origin is not None


def _ollama_returning(payload: object):
    response = MagicMock()
    response.read.return_value = json.dumps({"response": json.dumps(payload)}).encode()
    response.__enter__.return_value = response
    return patch("circa.llm.urlopen", return_value=response)


def test_string_list_fields_from_ollama_flow_through_to_a_pending_draft(tmp_path: Path) -> None:
    path = tmp_path / "team.json"
    payload = _diagnosis(confidence=0.5).model_dump() | {"missing_evidence": "None", "validation_steps": "Re-sign."}

    with _ollama_returning(payload):
        result = runner.invoke(app, ["investigate-log", str(UNKNOWN_LOG), "--knowledge", str(path)])

    assert result.exit_code == 0, result.output
    [draft] = json.loads(path.read_text())
    assert draft["status"] == "pending_review"
    assert draft["fix"]["validation_status"] == "unvalidated"
    assert draft["draft_origin"]["missing_evidence"] == []
    assert draft["draft_origin"]["validation_steps"] == ["Re-sign."]


@pytest.mark.parametrize(
    "payload",
    [{"category": "software", "confidence": 0.5, "root_cause": "x", "missing_evidence": {"a": 1}}, "free text"],
    ids=["invalid-list", "not-an-object"],
)
def test_invalid_ollama_diagnosis_falls_back_without_writing_knowledge(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "team.json"

    with _ollama_returning(payload):
        result = runner.invoke(app, ["investigate-log", str(UNKNOWN_LOG), "--knowledge", str(path), "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["outcome"] == "deterministic_only"
    assert report["analysis"] == json.loads(analyze_log(UNKNOWN_LOG.read_text()).model_dump_json(exclude_none=True))
    assert "Local LLM unavailable or returned an invalid response" in result.output
    assert not path.exists()


def test_zero_confidence_diagnosis_is_shown_but_not_drafted(tmp_path: Path) -> None:
    # The confirmed qwen2.5:1.5b reply: it echoes "insufficient evidence" with zero confidence.
    payload = _diagnosis(category="software", confidence=0.0).model_dump() | {"missing_evidence": "None"}
    path = tmp_path / "team.json"

    with _ollama_returning(payload):
        result = runner.invoke(app, ["investigate-log", str(UNKNOWN_LOG), "--knowledge", str(path), "--json"])

    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["outcome"] == "deterministic_only"
    assert report["diagnosis"]["missing_evidence"] == []
    assert "draft" not in report
    assert not path.exists()


def test_known_solution_is_still_returned_before_ollama_is_contacted(tmp_path: Path) -> None:
    path = _verified_example(tmp_path)

    with patch("circa.llm.urlopen") as urlopen:
        result = runner.invoke(app, ["investigate-log", str(DISK_LOG), "--knowledge", str(path), "--json"])

    assert json.loads(result.stdout)["outcome"] == "known_solution"
    urlopen.assert_not_called()
