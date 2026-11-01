import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from circa.cli import app
from circa.evaluation import (
    CaseResult,
    calculate_classification_metrics,
    load_classification_cases,
    run_classification_evaluation,
)
from circa.knowledge import load_incidents
from circa.resources import data_path

runner = CliRunner()
REPOSITORY = Path(__file__).parents[1]
INCIDENTS = load_incidents(data_path("incidents.json"))


def _result(expected: str, predicted: str, **fields: object) -> CaseResult:
    return CaseResult.model_validate(
        {
            "id": f"{expected}-{predicted}-{len(fields)}",
            "source": "synthetic",
            "label_status": "confirmed",
            "expected_category": expected,
            "predicted_category": predicted,
            "correct": expected == predicted,
            "confidence": 0.0 if predicted == "unknown" else 0.7,
            "root_cause": "x",
            "matched_incident_ids": [],
            **fields,
        }
    )


def test_metrics_state_counts_and_denominators() -> None:
    metrics = calculate_classification_metrics(
        [
            _result("hardware", "hardware", match_correct=True, root_cause_correct=True, llm_called=True),
            _result("hardware", "unknown", match_correct=False, llm_called=True),
            _result("software", "hardware", root_cause_correct=False, llm_called=False),
            _result("unknown", "unknown"),
            _result("unknown", "network"),
        ]
    )

    assert (metrics.accuracy.count, metrics.accuracy.total) == (2, 5)
    assert (metrics.unknown.count, metrics.unknown.total) == (2, 5)
    assert (metrics.false_positives.count, metrics.false_positives.total) == (1, 2)
    assert (metrics.wrong_category.count, metrics.wrong_category.total) == (2, 3)
    assert (metrics.matching.count, metrics.matching.total) == (1, 2)
    assert (metrics.root_cause.count, metrics.root_cause.total) == (1, 2)
    assert (metrics.llm_asked.count, metrics.llm_asked.total) == (2, 3)
    assert metrics.llm_asked_when_correct == 1
    hardware = next(row for row in metrics.per_category if row.category == "hardware")
    assert (hardware.support, hardware.predicted, hardware.precision, hardware.recall) == (2, 2, 0.5, 0.5)
    infrastructure = next(row for row in metrics.per_category if row.category == "infrastructure")
    assert infrastructure.precision is None and infrastructure.recall is None
    assert metrics.confusion["unknown"]["network"] == 1


def test_empty_groups_have_undefined_rates() -> None:
    metrics = calculate_classification_metrics([])

    assert metrics.cases == 0
    assert metrics.accuracy.rate is None and metrics.false_positives.rate is None


def _dataset(tmp_path: Path, cases: list[dict[str, object]]) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    return path


def _case(identifier: str, **fields: object) -> dict[str, object]:
    return {
        "id": identifier,
        "source": "synthetic",
        "label_status": "confirmed",
        "expected_category": "hardware",
        "label_note": "test",
        **fields,
    }


@pytest.mark.parametrize(
    ("case", "message"),
    [
        (_case("none"), "exactly one of"),
        (_case("both", failure_log="x", failure_text="y"), "exactly one of"),
        (_case("pending-synthetic", failure_log="x", label_status="pending_review"), "confirmed by construction"),
        (_case("outside", source="real", log_file="../secret.log"), "inside"),
        (_case("missing", source="real", log_file="logs/absent.log"), "inside"),
        (_case("bad-incident", failure_log="x", relevant_incident_ids=["nope"]), "unknown incident"),
        (_case("eval-hw-01", failure_log="x"), "Duplicate"),
        (_case("extra", failure_log="x", verified=True), "Extra inputs"),
    ],
    ids=[
        "no-input",
        "two-inputs",
        "pending-synthetic",
        "path-traversal",
        "missing-log",
        "unknown-incident",
        "duplicate-of-legacy",
        "unknown-field",
    ],
)
def test_invalid_cases_are_rejected(tmp_path: Path, case: dict[str, object], message: str) -> None:
    (tmp_path.parent / "secret.log").write_text("disk full", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_classification_cases(_dataset(tmp_path, [case]), INCIDENTS)


def test_dataset_must_be_an_object_with_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(TypeError, match="'cases' list"):
        load_classification_cases(path, INCIDENTS)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read"):
        load_classification_cases(path, INCIDENTS)


def test_evaluation_runs_logs_and_reports_pending_labels_separately(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "oom.log").write_text(
        "[  412.118] Out of memory: Killed process 8123 (python)\n##[error]Process completed with exit code 137.\n",
        encoding="utf-8",
    )
    path = _dataset(
        tmp_path,
        [
            _case(
                "real-oom",
                source="real",
                label_status="pending_review",
                log_file="logs/oom.log",
                relevant_incident_ids=["synthetic-hardware-memory-002"],
                expected_root_cause_terms=["memory"],
            ),
            _case("syn-unknown", failure_log="##[error]The operation was canceled.\n", expected_category="unknown"),
        ],
    )

    report = run_classification_evaluation(path, include_legacy=False)

    oom = next(case for case in report.cases if case.id == "real-oom")
    assert oom.predicted_category == "hardware" and oom.match_correct and oom.root_cause_correct
    assert oom.llm_called is True  # investigate-log asks the optional LLM even when this is already correct
    assert report.groups["pending_review"].cases == 1
    assert report.groups["all_confirmed"].cases == 1
    assert report.groups["pending_review"].llm_asked_when_correct == 1


def test_legacy_cases_reproduce_the_analyze_path() -> None:
    report = run_classification_evaluation(REPOSITORY / "evaluation" / "cases.json")
    legacy = [case for case in report.cases if case.source == "legacy"]

    assert len(legacy) == 35
    assert all(case.llm_called is None for case in legacy)  # structured text is not a log


def test_shipped_dataset_is_valid_and_keeps_real_and_synthetic_labels_apart() -> None:
    cases = load_classification_cases(REPOSITORY / "evaluation" / "cases.json", INCIDENTS, include_legacy=False)

    assert {case.source for case in cases} == {"real", "synthetic"}
    assert all(case.log_file for case in cases if case.source == "real")
    assert all(case.label_note for case in cases)


def test_evaluate_cli_prints_a_summary_and_json_without_touching_team_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = tmp_path / "team" / "incidents.json"
    team.parent.mkdir()
    team.write_text("[]\n", encoding="utf-8")
    before = team.stat().st_mtime_ns
    monkeypatch.chdir(tmp_path)
    dataset = str(REPOSITORY / "evaluation" / "cases.json")

    text = runner.invoke(app, ["evaluate", "--dataset", dataset])
    as_json = runner.invoke(app, ["evaluate", "--dataset", dataset, "--json"])

    assert text.exit_code == 0, text.output
    assert "Legacy synthetic cases (baseline): 35 case(s)" in text.output
    assert "labels pending review" in text.output and "Root cause missing the labeled terms:" in text.output
    payload = json.loads(as_json.output)
    assert set(payload["groups"]) == {"all_confirmed", "legacy", "synthetic", "real", "pending_review"}
    assert payload["groups"]["legacy"]["accuracy"]["total"] == 35
    assert team.read_text(encoding="utf-8") == "[]\n" and team.stat().st_mtime_ns == before


def test_evaluate_cli_reports_a_missing_dataset(tmp_path: Path) -> None:
    result = runner.invoke(app, ["evaluate", "--dataset", str(tmp_path / "absent.json")])

    assert result.exit_code == 1
    assert "Cannot read evaluation dataset" in result.output
