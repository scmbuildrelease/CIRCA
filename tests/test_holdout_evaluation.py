import importlib.util
import json
import sys
from pathlib import Path

from circa.knowledge_store import load_knowledge
from circa.models import HistoricalIncident

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("holdout", ROOT / "evaluation" / "holdout.py")
assert _spec is not None and _spec.loader is not None
holdout = importlib.util.module_from_spec(_spec)
sys.modules["holdout"] = holdout  # dataclasses look up their module while it is executing
_spec.loader.exec_module(holdout)

TEAM_FILE = ROOT / "team" / "incidents.json"


def test_no_failure_can_match_its_own_incident_and_knowledge_is_not_written() -> None:
    before = TEAM_FILE.read_bytes()

    results, skipped = holdout.evaluate(load_knowledge(TEAM_FILE).incidents, log_dir=None)

    assert TEAM_FILE.read_bytes() == before
    excluded = [r for r in results if r.excluded]
    assert excluded
    assert all(not set(r.excluded) & {*r.ranked_matches, r.matched_incident, r.root_cause_from} for r in excluded)
    # Raw logs of unseen runs are not in the repository; without --log-dir they are reported, not guessed.
    assert skipped == [q.id for q in holdout.QUERIES if q.log.startswith("run:")]


def test_logs_from_one_run_are_one_incident_and_pending_records_are_not_ground_truth() -> None:
    results, _ = holdout.evaluate(load_knowledge(TEAM_FILE).incidents, log_dir=None)
    summary = holdout.summarize(results)

    by_id = {r.id: r for r in results}
    assert by_id["circa-39988949831-prettier"].excluded == ["real-circa-ci-quality-gate-baseline-001"]
    # The tt-metal record names its run only in a fix reference, and is still pending review.
    assert by_id["tt-metal-protected-tag"].excluded == ["real-tt-metal-protected-tag-update-001"]
    assert not by_id["tt-metal-protected-tag"].ground_truth
    truth = summary["verified_ground_truth"]
    assert (truth["logs"], truth["independent_incidents"]) == ("4", "1")  # the four job logs of run 39988949831


def test_a_newly_verified_draft_is_ground_truth_and_excluded_from_its_own_evaluation() -> None:
    draft = json.loads((ROOT / "team" / "drafts" / "circa-ruff-import-sort.json").read_text(encoding="utf-8"))
    fix = draft["fix"] | {
        "validation_status": "validated",
        "validation_method": "manual_verification",
        "validation_evidence": "Test value only: simulates a reviewer's verification.",
    }
    verified = HistoricalIncident.model_validate(draft | {"status": "verified", "reviewed_by": "reviewer", "fix": fix})

    results, _ = holdout.evaluate([*load_knowledge(TEAM_FILE).incidents, verified], log_dir=None)

    result = next(r for r in results if r.source == "scmbuildrelease/CIRCA#39859422492")
    assert result.ground_truth and result.excluded == [verified.id]
    assert verified.id not in result.ranked_matches and result.root_cause_from != verified.id
