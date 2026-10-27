import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from circa.cli import app
from circa.knowledge_store import load_knowledge
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


DISK_LINE = "2026-10-22T10:14:16Z [ERROR] linker output could not be written: no space left on device"


def _disk_diagnosis(**overrides: object) -> LLMDiagnosis:
    fields = {
        "category": "hardware",
        "root_cause": "Hypothesis: the worker filesystem is full, so there is no space left on device.",
        "evidence": [DISK_LINE],
        "suggested_fix": "Free space on the worker filesystem.",
        "validation_steps": ["Confirm the worker filesystem has free space before the linker runs."],
    }
    return _diagnosis(**(fields | overrides))


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
    provider = FakeProvider(_disk_diagnosis())

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
    verify = [
        "knowledge",
        "verify",
        draft_id,
        "--knowledge",
        str(path),
        "--reviewer",
        "r",
        "--yes",
        "--category",
        "infrastructure",
    ]

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
    verify = [
        "knowledge",
        "verify",
        draft_id,
        "--knowledge",
        str(path),
        "--reviewer",
        "r",
        "--yes",
        "--category",
        "infrastructure",
    ]
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
        FakeProvider(_diagnosis(root_cause=f"Certificate chain expired; the job used {secret}.")),
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
    result = _invoke(FakeProvider(_disk_diagnosis()), str(DISK_LOG), "--json")
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
    payload = _diagnosis(confidence=0.5).model_dump() | {
        "missing_evidence": "None",
        "validation_steps": "Re-sign the bundle with a renewed certificate.",
    }

    with _ollama_returning(payload):
        result = runner.invoke(app, ["investigate-log", str(UNKNOWN_LOG), "--knowledge", str(path)])

    assert result.exit_code == 0, result.output
    [draft] = json.loads(path.read_text())
    assert draft["status"] == "pending_review"
    assert draft["fix"]["validation_status"] == "unvalidated"
    assert draft["draft_origin"]["missing_evidence"] == []
    assert draft["draft_origin"]["validation_steps"] == ["Re-sign the bundle with a renewed certificate."]


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


def _investigate(diagnosis: LLMDiagnosis, log: Path = UNKNOWN_LOG):
    return investigate_log(log.read_text(), [], FakeProvider(diagnosis), source_name=log.name)


def test_low_but_nonzero_confidence_still_drafts_a_grounded_hypothesis() -> None:
    investigation = _investigate(_diagnosis(confidence=0.05))

    assert investigation.outcome == "new_diagnosis"
    assert investigation.draft is not None
    assert investigation.draft["draft_origin"]["confidence"] == 0.05


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"evidence": []}, "quoted no line that appears in the log"),
        ({"evidence": ["disk quota exceeded on build volume"]}, "quoted no line that appears in the log"),
        ({"evidence": ["expired"]}, "quoted no line that appears in the log"),  # a fragment, not a log fact
        (
            {"root_cause": "Insufficient deterministic evidence to identify a root cause."},
            "root cause is not grounded in the log",
        ),
        ({"root_cause": "A flaky network switch dropped packets."}, "root cause is not grounded in the log"),
        ({"confidence": 0.0}, "zero confidence"),
        ({"category": "unknown"}, "no category was identified"),
    ],
    ids=["no-evidence", "invented-evidence", "fragment", "placeholder-cause", "unrelated-cause", "zero", "unknown"],
)
def test_unsupported_diagnoses_do_not_create_drafts(overrides: dict, reason: str) -> None:
    investigation = _investigate(_diagnosis(**overrides))

    assert investigation.outcome == "deterministic_only"
    assert investigation.draft is None
    assert any(reason in note for note in investigation.notes)


@pytest.mark.parametrize(
    ("fix", "reason"),
    [
        ("Upgrade the Python interpreter on all runners.", "is unrelated to the log"),
        ("Allow unsigned bundles in the firmware packaging policy.", "would weaken a safeguard"),
        ("Disable certificate chain checks in signtool.", "would weaken a safeguard"),
    ],
    ids=["unrelated", "contradicts-log", "disables-check"],
)
def test_unsupported_or_unsafe_fixes_are_kept_out_of_the_draft(fix: str, reason: str) -> None:
    unsafe_step = "Skip certificate validation when packaging the bundle."
    diagnosis = _diagnosis(suggested_fix=fix, validation_steps=[unsafe_step, "Confirm signtool accepts the bundle."])

    investigation = _investigate(diagnosis)

    draft = investigation.draft
    assert draft is not None
    assert "fix" not in draft
    assert draft["recommendation"] == investigation.analysis.recommendation
    assert draft["draft_origin"]["validation_steps"] == ["Confirm signtool accepts the bundle."]
    assert any(reason in note and fix in note for note in investigation.notes)
    assert any(unsafe_step in note for note in investigation.notes)


def test_draft_records_category_and_evidence_provenance() -> None:
    quote = SIGNTOOL_LINE.split("] ", 1)[1]
    llm_category = _investigate(_diagnosis(evidence=[quote, "made up line"]))
    historical = _investigate(_disk_diagnosis(category="software"), DISK_LOG)

    origin = llm_category.draft["draft_origin"]
    assert llm_category.analysis.category == "unknown"
    assert origin["category_source"] == "llm"
    assert origin["cited_log_lines"] == [quote]
    assert llm_category.draft["evidence"][0] == SIGNTOOL_LINE  # facts come from the log, not the model
    assert origin["missing_evidence"]  # recorded as a qualification, not a blocker
    assert historical.draft["category"] == "hardware"
    assert historical.draft["draft_origin"]["category_source"] == "historical"
    assert "LLM suggested software; keeping deterministic category hardware." in historical.notes


def test_lifecycle_from_unknown_failure_to_reused_verified_knowledge(tmp_path: Path) -> None:
    path = tmp_path / "team.json"
    knowledge = ["--knowledge", str(path)]

    # 1–2. Unknown failure: one LLM call creates a pending_review draft.
    first = FakeProvider(_diagnosis())
    assert _invoke(first, str(UNKNOWN_LOG), *knowledge).exit_code == 0
    [draft] = json.loads(path.read_text())
    assert len(first.prompts) == 1 and draft["status"] == "pending_review"

    # 3. Pending drafts are never matched by analysis.
    before = json.loads(runner.invoke(app, ["analyze-log", str(UNKNOWN_LOG), *knowledge, "--json"]).output)
    assert draft["id"] not in before["matched_incident_ids"]

    # 4. Explicit approval with the reviewer's own validation evidence.
    approve = [
        "knowledge",
        "verify",
        draft["id"],
        *knowledge,
        "--reviewer",
        "r",
        "--yes",
        "--category",
        "infrastructure",
    ]
    evidence = ["--validation-method", "manual_verification", "--validation-evidence", "Re-signed and packaged."]
    assert runner.invoke(app, [*approve, *evidence]).exit_code == 0

    # 5–6. A later run of the same failure is answered from verified knowledge without the LLM.
    later = tmp_path / "later.log"
    later.write_text(UNKNOWN_LOG.read_text().replace("2026-10-24T09:30", "2026-11-02T17:45").replace("03", "07"))
    second = FakeProvider(_diagnosis())
    result = _invoke(second, str(later), *knowledge, "--json")
    assert json.loads(result.stdout)["outcome"] == "known_solution"
    assert json.loads(result.stdout)["matched_incident"]["id"] == draft["id"]
    assert second.prompts == []

    # 7. Invalid LLM output for a new failure writes nothing.
    other = tmp_path / "other.log"
    other.write_text("2026-11-02T18:00:00Z [ERROR] artifact checksum mismatch for firmware image\n")
    approved = path.read_bytes()
    with _ollama_returning({"category": "software", "confidence": "high", "root_cause": "x"}):
        invalid = runner.invoke(app, ["investigate-log", str(other), *knowledge])
    assert invalid.exit_code == 0 and path.read_bytes() == approved


# Regression cases replay qwen2.5:1.5b output observed in the pilot (see README "Piloting on real logs").
TT_METAL_LOG = Path(__file__).parent / "fixtures" / "real_incidents" / "tt-metal-protected-tag.log"


def test_suggestions_using_matched_history_wording_are_kept() -> None:
    # The log says "device" and "filesystem"; the matched disk incident says "disk" and "worker".
    diagnosis = _diagnosis(
        category="hardware",
        root_cause="no space left on device",
        evidence=["ERROR: no space left on device"],
        suggested_fix="Increase worker disk space or free up space on the worker disk.",
        validation_steps=["Check the worker disk space usage", "Upgrade the Python interpreter on all runners."],
    )

    investigation = _investigate(diagnosis, EXAMPLES / "hardware_failure.log")

    draft = investigation.draft
    assert draft is not None and "synthetic-hardware-disk-001" in investigation.analysis.matched_incident_ids
    assert draft["fix"]["description"] == "Increase worker disk space or free up space on the worker disk."
    assert draft["draft_origin"]["validation_steps"] == ["Check the worker disk space usage"]


def test_plural_wording_counts_as_grounded() -> None:
    diagnosis = _diagnosis(
        category="infrastructure",
        root_cause="Repository rule violations found",
        evidence=["HttpError: Repository rule violations found"],
        suggested_fix="Check and resolve repository rule violations.",
        validation_steps=["Verify repository rules are up-to-date and correctly configured."],
    )

    investigation = _investigate(diagnosis, TT_METAL_LOG)

    assert investigation.draft is not None
    assert investigation.draft["draft_origin"]["validation_steps"] == diagnosis.validation_steps


def test_matched_history_never_rescues_a_safeguard_weakening_fix() -> None:
    diagnosis = _disk_diagnosis(suggested_fix="Skip the worker disk space check before the build.")

    investigation = _investigate(diagnosis, DISK_LOG)

    assert investigation.draft is not None and "fix" not in investigation.draft
    assert any("would weaken a safeguard" in note for note in investigation.notes)


def test_json_output_stays_valid_when_a_draft_is_saved(tmp_path: Path) -> None:
    path = tmp_path / "team.json"

    result = _invoke(FakeProvider(_diagnosis()), str(UNKNOWN_LOG), "--knowledge", str(path), "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["outcome"] == "new_diagnosis"
    assert "Draft saved: draft-" in result.stderr


TEAM_FILE = Path(__file__).parents[1] / "team" / "incidents.json"


def _verify(path: Path, incident_id: str, *extra: str):
    command = ["knowledge", "verify", incident_id, "--knowledge", str(path), "--reviewer", "r", "--yes", *extra]
    return runner.invoke(app, command)


def test_llm_category_must_be_confirmed_or_corrected_at_approval(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path, "--synthetic")
    assert json.loads(path.read_text())[0]["draft_origin"]["category_source"] == "llm"

    unconfirmed = _verify(path, draft_id)
    assert unconfirmed.exit_code == 1 and "suggested by the LLM" in unconfirmed.output
    assert json.loads(path.read_text())[0]["status"] == "pending_review"

    corrected = _verify(path, draft_id, "--category", "software")
    [record] = json.loads(path.read_text())
    assert corrected.exit_code == 0
    assert (record["status"], record["category"]) == ("verified", "software")
    assert record["draft_origin"]["category_source"] == "reviewer"
    assert record["fix"]["validation_status"] == "unvalidated"  # confirming a category validates nothing


def test_invalid_reviewer_category_is_rejected(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path, "--synthetic")

    result = _verify(path, draft_id, "--category", "cosmic-rays")

    assert result.exit_code == 1 and "category" in result.output
    assert json.loads(path.read_text())[0]["status"] == "pending_review"


def test_editing_the_category_records_reviewer_provenance_and_cannot_forge_it(tmp_path: Path) -> None:
    path, draft_id = _drafted(tmp_path, "--synthetic")
    show = runner.invoke(app, ["knowledge", "show", draft_id, "--knowledge", str(path), "--json"])
    draft = json.loads(show.output)
    edited = tmp_path / "edited.json"

    # Claiming a deterministic source without changing anything is ignored.
    draft["draft_origin"]["category_source"] = "deterministic"
    edited.write_text(json.dumps(draft))
    assert runner.invoke(app, ["knowledge", "update", str(edited), "--knowledge", str(path), "--yes"]).exit_code == 0
    assert json.loads(path.read_text())[0]["draft_origin"]["category_source"] == "llm"

    # Changing the category is a reviewer decision.
    edited.write_text(json.dumps(draft | {"category": "software"}))
    assert runner.invoke(app, ["knowledge", "update", str(edited), "--knowledge", str(path), "--yes"]).exit_code == 0
    assert json.loads(path.read_text())[0]["draft_origin"]["category_source"] == "reviewer"
    assert _verify(path, draft_id).exit_code == 0


def test_deterministic_categories_and_legacy_records_need_no_category_confirmation(tmp_path: Path) -> None:
    draft = _investigate(_disk_diagnosis(), DISK_LOG).draft
    assert draft is not None and draft["draft_origin"]["category_source"] == "historical"
    # Records without draft_origin (written before provenance existed) still verify as before.
    assert runner.invoke(app, ["knowledge", "validate", str(_verified_example(tmp_path))]).exit_code == 0


def test_existing_team_knowledge_still_loads(tmp_path: Path) -> None:
    copy = tmp_path / "team.json"
    copy.write_bytes(TEAM_FILE.read_bytes())  # never touch the real file

    result = runner.invoke(app, ["knowledge", "validate", str(copy)])

    assert result.exit_code == 0 and "1 pending_review" in result.output
    assert copy.read_bytes() == TEAM_FILE.read_bytes()


def test_protected_tag_failure_matches_its_human_record_exactly(tmp_path: Path) -> None:
    copy = tmp_path / "team.json"
    copy.write_bytes(TEAM_FILE.read_bytes())
    provider = FakeProvider(_diagnosis())

    investigation = investigate_log(
        TT_METAL_LOG.read_text(), load_knowledge(copy).incidents, provider, source_name=TT_METAL_LOG.name
    )

    assert investigation.evidence == [
        "HttpError: Repository rule violations found",
        "Cannot update this protected ref.",
    ]
    assert investigation.outcome == "existing_record"
    assert investigation.match_reason == "same failure fingerprint"
    assert investigation.matched_incident.id == "real-tt-metal-protected-tag-update-001"
    assert provider.prompts == []


def test_performance_failure_is_not_drafted_as_an_unsupported_assertion_failure() -> None:
    log = EXAMPLES / "demo_performance_threshold.log"
    # Observed qwen2.5:1.5b output for this log: an assertion failure that the log never mentions.
    diagnosis = _diagnosis(
        category="software",
        confidence=0.95,
        root_cause="Assertion failure in software unit test",
        evidence=["2026-10-22T12:40:11Z [ERROR] PERFORMANCE THRESHOLD VIOLATION"],
        suggested_fix="Re-run the unit test to verify the regression and address the assertion failure.",
    )

    investigation = _investigate(diagnosis, log)

    assert investigation.draft is None
    assert any("root cause is not grounded" in note for note in investigation.notes)
    assert investigation.analysis.performance_assessment is not None
    assert investigation.analysis.performance_assessment.failure_type == "performance_threshold_violation"


# Verbatim lines from `gh run view 39988949831 --log-failed` (scmbuildrelease/CIRCA, ANSI colors removed).
CI_GATE_EVIDENCE = [
    (
        "Format (Prettier)\tCheck Markdown, YAML, and JSON formatting\t2026-10-23T16:42:11.1942883Z [warn] Code style "
        "issues found in 3 files. Run Prettier with --write to fix."
    ),
    (
        "Lint (Ruff)\tRuff format check\t2026-10-23T16:42:10.9226749Z 12 files would be reformatted, 22 files already "
        "formatted"
    ),
    (
        "Security (Bandit)\tRun Bandit\t2026-10-23T16:42:13.8758687Z >> Issue: [B310:blacklist] Audit url open for "
        "permitted schemes. Allowing use of file:/ or custom schemes is often unexpected."
    ),
    "Type check (mypy)\tRun mypy\t2026-10-23T16:42:24.2208760Z Found 9 errors in 6 files (checked 17 source files)",
]


def _verified_ci_gate_incident(tmp_path: Path) -> Path:
    draft = {
        "id": "real-ci-gate-baseline-001",
        "title": "New CI quality gate failed on pre-existing formatting, typing, and Bandit findings",
        "category": "software",
        "summary": "Prettier found 3 unformatted files, Ruff format 12 files, mypy 9 type errors, and Bandit B310.",
        "keywords": ["quality gate", "Prettier", "ruff format", "would be reformatted", "mypy", "Bandit", "B310"],
        "recommendation": "Bring existing code into compliance in the change that enables a new check.",
        "synthetic": False,
        "evidence": CI_GATE_EVIDENCE,
        "root_cause": "The quality gate was enabled without bringing the existing code into compliance.",
        "fix": {"description": "Formatted the files, fixed the mypy errors, and restricted urlopen to http(s)."},
    }
    draft_path, path = tmp_path / "draft.json", tmp_path / "team.json"
    draft_path.write_text(json.dumps(draft))
    assert runner.invoke(app, ["knowledge", "add", str(draft_path), "--knowledge", str(path), "--yes"]).exit_code == 0
    evidence = [
        "--validation-method",
        "reproduced_then_fixed",
        "--validation-evidence",
        "Checks fail before, pass after.",
    ]
    assert _verify(path, "real-ci-gate-baseline-001", *evidence).exit_code == 0
    return path


def test_real_ci_gate_failure_reuses_its_verified_record_without_the_llm(tmp_path: Path) -> None:
    path = _verified_ci_gate_incident(tmp_path)
    before = path.read_bytes()
    # A later run: other timestamps, plus unrelated noise around the same failures.
    later = tmp_path / "later.log"
    later.write_text(
        "\n".join(
            [
                "Lint (Ruff)\tRuff lint\t2026-11-04T08:17:01.0000000Z All checks passed!",
                *(line.replace("2026-10-23T16:42", "2026-11-04T08:17") for line in CI_GATE_EVIDENCE),
                "Type check (mypy)\tRun mypy\t2026-11-04T08:17:24.3000000Z ##[error]Process completed with exit code 1.",
            ]
        )
    )
    provider = FakeProvider(_diagnosis())

    result = _invoke(provider, str(later), "--knowledge", str(path), "--json")

    report = json.loads(result.stdout)
    assert report["outcome"] == "known_solution"
    assert report["matched_incident"]["id"] == "real-ci-gate-baseline-001"
    assert report["matched_incident"]["fix"]["validation_status"] == "validated"
    assert provider.prompts == []
    assert path.read_bytes() == before  # no duplicate draft


def test_keyword_similarity_without_the_recorded_evidence_is_not_a_known_solution(tmp_path: Path) -> None:
    path = _verified_ci_gate_incident(tmp_path)
    # A different mypy failure shares job, step, and tool words but none of the recorded evidence.
    other = tmp_path / "other.log"
    other.write_text(
        "Type check (mypy)\tRun mypy\t2026-11-05T10:00:00.0000000Z src/app.py:12: error: Missing return statement\n"
        "Type check (mypy)\tRun mypy\t2026-11-05T10:00:00.1000000Z Found 1 error in 1 file (checked 18 source files)\n"
    )
    provider = FakeProvider(_diagnosis())

    report = json.loads(_invoke(provider, str(other), "--knowledge", str(path), "--json").stdout)

    assert report["outcome"] not in {"known_solution", "existing_record"}
    assert len(provider.prompts) == 1  # diagnosed, not answered with an unrelated verified fix
