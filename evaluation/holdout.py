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
    source: str  # "owner/repo#run": logs from one run are one incident
    expected_category: CauseCategory  # used only when no verified team incident records this run
    seen_in_development: bool  # whether the log was available when the current rules were written


# Every failed run in scmbuildrelease/CIRCA up to 2026-11-01, plus the tt-metal log already in evaluation/.
# Whether a case is ground truth, and its expected category, come from team/incidents.json: only a verified
# incident recorded from the same run counts.
QUERIES = [
    Query("circa-39988949831-prettier", "logs/circa-39988949831-prettier.log",
          "scmbuildrelease/CIRCA#39988949831", "software", True),
    Query("circa-39988949831-ruff-format", "logs/circa-39988949831-ruff-format.log",
          "scmbuildrelease/CIRCA#39988949831", "software", True),
    Query("circa-39988949831-bandit", "logs/circa-39988949831-bandit.log",
          "scmbuildrelease/CIRCA#39988949831", "software", True),
    Query("circa-39988949831-mypy", "logs/circa-39988949831-mypy.log",
          "scmbuildrelease/CIRCA#39988949831", "software", True),
    Query("circa-41331905922-prettier-scratch-files", "run:41331905922",
          "scmbuildrelease/CIRCA#41331905922", "software", False),
    Query("circa-41345998382-zizmor-advisory", "run:41345998382",
          "scmbuildrelease/CIRCA#41345998382", "software", False),
    Query("circa-39859422492-ruff-import-sort", "logs/circa-39859422492-ruff-import-sort.log",
          "scmbuildrelease/CIRCA#39859422492", "software", True),
    Query("circa-41312342881-pytest-assertion", "logs/circa-41312342881-pytest-assertion.log",
          "scmbuildrelease/CIRCA#41312342881", "software", True),
    Query("tt-metal-protected-tag", "logs/tt-metal-protected-tag.log",
          "tenstorrent/tt-metal#40072645356", "infrastructure", True),
]  # fmt: skip


@dataclass
class QueryResult:
    id: str
    source: str
    ground_truth: bool  # a verified team incident records this run
    seen_in_development: bool
    excluded: list[str]  # team incidents recorded from this run, removed before searching
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


def recorded_from(incident: HistoricalIncident, source: str) -> bool:
    """Whether a team incident was recorded from this run: its environment or a fix reference names it."""

    repository, run = source.split("#")
    environment = incident.environment
    if environment.get("repository") == repository and environment.get("run") == run:
        return True
    references = incident.fix.references if incident.fix else []
    return any(f"github.com/{repository}/actions/runs/{run}" in reference for reference in references)


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
        own = [incident for incident in team if recorded_from(incident, query.source)]
        verified = [incident for incident in own if incident.status == "verified"]
        excluded = {incident.id for incident in own}
        remaining = [incident for incident in team if incident.id not in excluded]
        investigation = investigate_log(raw_log, remaining, None, source_name=query.id)
        matches = investigation.analysis.matched_incident_ids
        matched = investigation.matched_incident
        root_cause_from = root_causes.get(investigation.analysis.root_cause)
        assert not excluded & {*matches, matched.id if matched else None, root_cause_from}
        expected = verified[0].category if verified else query.expected_category
        results.append(
            QueryResult(
                id=query.id,
                source=query.source,
                ground_truth=bool(verified),
                seen_in_development=query.seen_in_development,
                excluded=sorted(excluded),
                expected_category=expected,
                predicted_category=investigation.analysis.category,
                category_correct=investigation.analysis.category == expected,
                ranked_matches=matches,
                team_matches=[incident_id for incident_id in matches if incident_id in team_ids],
                root_cause_from=root_cause_from,
                outcome=investigation.outcome,
                matched_incident=matched.id if matched else None,
                match_reason=investigation.match_reason,
            )
        )
    return results, skipped


def _fraction(count: int, total: int) -> str:
    return f"{count}/{total}" + (f" ({count / total:.0%})" if total else " (n/a)")


def summarize(results: list[QueryResult]) -> dict[str, dict[str, str]]:
    """Metrics per group, over logs and over independent incidents (logs from one run are one incident).

    An incident counts as correct only if all of its logs are, and as a false match if any of its logs is.
    No held-out failure has another relevant team incident, so any team match is false.
    """

    checks = {
        "category_correct": lambda r: r.category_correct,
        "unknown_category": lambda r: r.predicted_category == "unknown",
        "team_incident_matched (all false)": lambda r: bool(r.team_matches),
        "other_incident_root_cause_reported (wrong)": lambda r: r.root_cause_from is not None,
        "packaged_synthetic_matched (see cases.json labels)": lambda r: len(r.ranked_matches) > len(r.team_matches),
        "known_solution_returned (wrong fix)": lambda r: r.outcome == "known_solution",
    }
    groups = {
        "verified_ground_truth": [r for r in results if r.ground_truth],
        "verified_unseen_in_development": [r for r in results if r.ground_truth and not r.seen_in_development],
        "not_ground_truth (no verified incident for the run)": [r for r in results if not r.ground_truth],
    }
    summary: dict[str, dict[str, str]] = {}
    for name, group in groups.items():
        incidents: dict[str, list[QueryResult]] = {}
        for result in group:
            incidents.setdefault(result.source, []).append(result)
        metrics = {"logs": str(len(group)), "independent_incidents": str(len(incidents))}
        for metric, check in checks.items():
            per_incident = all if metric == "category_correct" else any
            metrics[metric] = (
                f"logs {_fraction(sum(map(check, group)), len(group))}; "
                f"incidents {_fraction(sum(per_incident(map(check, logs)) for logs in incidents.values()), len(incidents))}"
            )
        summary[name] = metrics
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
