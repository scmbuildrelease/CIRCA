"""Leave-one-out evaluation of CIRCA on real CI failures.

Each real failure log is investigated with its own team incident removed from the
searchable knowledge, so it can never match itself. Team knowledge is read, never
written; the removal happens on an in-memory copy.

Usage (from the repository root):

    python evaluation/holdout.py [--log-dir DIR] [--json]

Logs of failures that were not seen while CIRCA's rules were written are not stored in
the repository (they are raw CI logs). Fetch them into DIR first; see
docs/phase-21-real-world-accuracy.md.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from circa.knowledge_store import load_knowledge
from circa.models import CauseCategory, HistoricalIncident
from circa.pipeline import investigate_log

ROOT = Path(__file__).resolve().parents[1]
TEAM_FILE = ROOT / "team" / "incidents.json"
# CIRCA removes ANSI colors when it fetches a run log itself (circa.github_runs); logs fetched with gh need the same.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Query:
    id: str
    log: str  # relative to evaluation/, or "run:<id>" for a log fetched into --log-dir
    expected_category: CauseCategory
    held_out: str | None  # the team incident this failure is the source of; removed before searching
    label: str  # verified_incident, fix_validated, or pending_review
    seen_in_development: bool  # whether the log was available when the current rules were written


# Every failed run in scmbuildrelease/CIRCA up to 2026-11-01, plus the tt-metal log already in evaluation/.
QUERIES = [
    Query("circa-39988949831-prettier", "logs/circa-39988949831-prettier.log", "software",
          "real-circa-ci-quality-gate-baseline-001", "verified_incident", True),
    Query("circa-39988949831-ruff-format", "logs/circa-39988949831-ruff-format.log", "software",
          "real-circa-ci-quality-gate-baseline-001", "verified_incident", True),
    Query("circa-39988949831-bandit", "logs/circa-39988949831-bandit.log", "software",
          "real-circa-ci-quality-gate-baseline-001", "verified_incident", True),
    Query("circa-39988949831-mypy", "logs/circa-39988949831-mypy.log", "software",
          "real-circa-ci-quality-gate-baseline-001", "verified_incident", True),
    Query("circa-41331905922-prettier-scratch-files", "run:41331905922", "software",
          "real-circa-ci-prettier-scratch-files-001", "verified_incident", False),
    Query("circa-41345998382-zizmor-advisory", "run:41345998382", "software",
          None, "fix_validated", False),
    Query("circa-39859422492-ruff-import-sort", "logs/circa-39859422492-ruff-import-sort.log", "software",
          None, "pending_review", True),
    Query("circa-41312342881-pytest-assertion", "logs/circa-41312342881-pytest-assertion.log", "software",
          None, "pending_review", True),
    Query("tt-metal-protected-tag", "logs/tt-metal-protected-tag.log", "infrastructure",
          "real-tt-metal-protected-tag-update-001", "pending_review", True),
]  # fmt: skip


@dataclass
class QueryResult:
    id: str
    label: str
    seen_in_development: bool
    held_out: str | None
    expected_category: str
    predicted_category: str
    category_correct: bool
    ranked_matches: list[str]  # the analyzer's historical matches, best first
    team_matches: list[str]  # the real team incidents among them
    root_cause_from: str | None  # a team incident whose root cause CIRCA reported as this failure's
    outcome: str
    matched_incident: str | None
    match_reason: str | None


def _read_log(query: Query, log_dir: Path | None) -> str | None:
    if query.log.startswith("run:"):
        if log_dir is None:
            return None
        path = log_dir / f"circa-{query.log.removeprefix('run:')}.log"
        return _ANSI.sub("", path.read_text(encoding="utf-8")) if path.exists() else None
    return (ROOT / "evaluation" / query.log).read_text(encoding="utf-8")


def evaluate(team: list[HistoricalIncident], log_dir: Path | None) -> tuple[list[QueryResult], list[str]]:
    team_ids = {incident.id for incident in team}
    root_causes = {incident.root_cause: incident.id for incident in team if incident.root_cause}
    results: list[QueryResult] = []
    skipped: list[str] = []
    for query in QUERIES:
        raw_log = _read_log(query, log_dir)
        if raw_log is None:
            skipped.append(query.id)
            continue
        remaining = [incident for incident in team if incident.id != query.held_out]
        assert all(incident.id != query.held_out for incident in remaining)
        investigation = investigate_log(raw_log, remaining, None, source_name=query.id)
        matches = investigation.analysis.matched_incident_ids
        matched = investigation.matched_incident
        assert query.held_out not in matches and (matched is None or matched.id != query.held_out)
        results.append(
            QueryResult(
                id=query.id,
                label=query.label,
                seen_in_development=query.seen_in_development,
                held_out=query.held_out,
                expected_category=query.expected_category,
                predicted_category=investigation.analysis.category,
                category_correct=investigation.analysis.category == query.expected_category,
                ranked_matches=matches,
                team_matches=[incident_id for incident_id in matches if incident_id in team_ids],
                root_cause_from=root_causes.get(investigation.analysis.root_cause),
                outcome=investigation.outcome,
                matched_incident=matched.id if matched else None,
                match_reason=investigation.match_reason,
            )
        )
    return results, skipped


def _fraction(count: int, total: int) -> str:
    return f"{count}/{total}" + (f" ({count / total:.0%})" if total else " (n/a)")


def summarize(results: list[QueryResult]) -> dict[str, dict[str, str]]:
    """Metrics per group. No held-out failure has another relevant team incident, so any team match is false."""

    groups = {
        "verified_or_fix_validated": [r for r in results if r.label != "pending_review"],
        "unseen_in_development": [r for r in results if not r.seen_in_development and r.label != "pending_review"],
        "pending_review": [r for r in results if r.label == "pending_review"],
    }
    summary: dict[str, dict[str, str]] = {}
    for name, group in groups.items():
        total = len(group)
        summary[name] = {
            "cases": str(total),
            "category_accuracy": _fraction(sum(r.category_correct for r in group), total),
            "unknown_category": _fraction(sum(r.predicted_category == "unknown" for r in group), total),
            "team_incident_matched (all false)": _fraction(sum(bool(r.team_matches) for r in group), total),
            "packaged_synthetic_matched (see cases.json labels)": _fraction(
                sum(len(r.ranked_matches) > len(r.team_matches) for r in group), total
            ),
            "other_incident_root_cause_reported (wrong)": _fraction(
                sum(r.root_cause_from is not None for r in group), total
            ),
            "no_historical_match": _fraction(sum(not r.ranked_matches for r in group), total),
            "known_solution_returned (wrong fix)": _fraction(sum(r.outcome == "known_solution" for r in group), total),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, help="Directory holding circa-<run id>.log files for unseen runs.")
    parser.add_argument("--json", action="store_true", help="Print per-case results and metrics as JSON.")
    args = parser.parse_args()

    before = TEAM_FILE.read_bytes()
    results, skipped = evaluate(load_knowledge(TEAM_FILE).incidents, args.log_dir)
    assert TEAM_FILE.read_bytes() == before, "team knowledge must not change"
    summary = summarize(results)

    if args.json:
        print(json.dumps({"summary": summary, "skipped": skipped, "cases": [asdict(r) for r in results]}, indent=2))
        return
    for result in results:
        print(
            f"{result.id}: expected {result.expected_category}, got {result.predicted_category}; "
            f"matches {result.ranked_matches or 'none'}; root cause from {result.root_cause_from or 'no team incident'}; "
            f"outcome {result.outcome}"
        )
    for name, metrics in summary.items():
        print(f"\n{name}")
        for metric, value in metrics.items():
            print(f"  {metric}: {value}")
    if skipped:
        print(f"\nSkipped (log not found; pass --log-dir): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
