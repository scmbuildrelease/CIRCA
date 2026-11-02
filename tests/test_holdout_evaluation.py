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

    results = holdout.evaluate(load_knowledge(TEAM_FILE).incidents)

    assert TEAM_FILE.read_bytes() == before
    excluded = [r for r in results if r.excluded]
    assert excluded
    assert all(not set(r.excluded) & {*r.ranked_matches, r.matched_incident, r.root_cause_from} for r in excluded)
    # Every case, including runs whose GitHub logs may have expired, is read from a preserved copy.
    assert [r.id for r in results] == [q.id for q in holdout.QUERIES]


def test_logs_from_one_run_are_one_incident_and_pending_records_are_not_ground_truth() -> None:
    results = holdout.evaluate(load_knowledge(TEAM_FILE).incidents)
    summary = holdout.summarize(results)

    by_id = {r.id: r for r in results}
    assert by_id["circa-39988949831-prettier"].excluded == ["real-circa-ci-quality-gate-baseline-001"]
    # The tt-metal record names its run only in a fix reference, and is still pending review.
    assert by_id["tt-metal-protected-tag"].excluded == ["real-tt-metal-protected-tag-update-001"]
    assert not by_id["tt-metal-protected-tag"].ground_truth
    truth = summary["verified_ground_truth"]
    # The four job logs of run 39988949831 are one incident; each other verified run is one more.
    assert (truth["logs"], truth["independent_incidents"]) == ("10", "7")
    # Pending records are never ground truth; the two excerpts of tt-metal run 40072645356 are one incident.
    pending = summary["not_ground_truth (no verified incident for the run)"]
    assert (pending["logs"], pending["independent_incidents"]) == ("2", "1")


def test_a_newly_verified_draft_is_ground_truth_and_excluded_from_its_own_evaluation() -> None:
    draft = json.loads((ROOT / "team" / "drafts" / "circa-ruff-import-sort.json").read_text(encoding="utf-8"))
    draft["id"] = "real-circa-ci-ruff-import-sort-test-copy"  # the real record is verified already
    fix = draft["fix"] | {
        "validation_status": "validated",
        "validation_method": "manual_verification",
        "validation_evidence": "Test value only: simulates a reviewer's verification.",
    }
    verified = HistoricalIncident.model_validate(draft | {"status": "verified", "reviewed_by": "reviewer", "fix": fix})

    results = holdout.evaluate([*load_knowledge(TEAM_FILE).incidents, verified])

    result = next(r for r in results if r.source == "scmbuildrelease/CIRCA#39859422492")
    assert result.ground_truth and verified.id in result.excluded
    assert verified.id not in result.ranked_matches and result.root_cause_from != verified.id
