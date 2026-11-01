import importlib.util
import sys
from pathlib import Path

from circa.knowledge_store import load_knowledge

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("holdout", ROOT / "evaluation" / "holdout.py")
assert _spec is not None and _spec.loader is not None
holdout = importlib.util.module_from_spec(_spec)
sys.modules["holdout"] = holdout  # dataclasses look up their module while it is executing
_spec.loader.exec_module(holdout)


def test_no_failure_can_match_its_own_incident_and_knowledge_is_not_written() -> None:
    team_file = ROOT / "team" / "incidents.json"
    before = team_file.read_bytes()

    results, skipped = holdout.evaluate(load_knowledge(team_file).incidents, log_dir=None)

    assert team_file.read_bytes() == before
    held_out = [r for r in results if r.held_out is not None]
    assert held_out and all(r.held_out not in r.ranked_matches for r in held_out)
    assert all(r.held_out not in (r.matched_incident, r.root_cause_from) for r in held_out)
    # Raw logs of unseen runs are not in the repository; without --log-dir they are reported, not guessed.
    assert skipped == [q.id for q in holdout.QUERIES if q.log.startswith("run:")]


def test_every_held_out_incident_exists_in_team_knowledge() -> None:
    team_ids = {incident.id for incident in load_knowledge(ROOT / "team" / "incidents.json").incidents}

    assert {q.held_out for q in holdout.QUERIES if q.held_out} <= team_ids
