"""Leave-one-out evaluation of CIRCA on real CI failures.

Each real failure log is investigated with its own team incident removed from the
searchable knowledge, so it can never match itself. Team knowledge is read, never
written; the removal happens on an in-memory copy.

Usage (from the repository root):

    python evaluation/holdout.py [--json]

Every log is a sanitized copy preserved in evaluation/logs/ (see evaluation/preserve.py),
so the evaluation does not depend on GitHub keeping Actions logs. Each log's run comes
from evaluation/logs/manifest.json, and its checksum is checked before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from circa.knowledge_store import load_knowledge
from circa.models import CauseCategory, HistoricalIncident
from circa.pipeline import investigate_log

ROOT = Path(__file__).resolve().parents[1]
TEAM_FILE = ROOT / "team" / "incidents.json"
LOG_DIR = ROOT / "evaluation" / "logs"


@dataclass(frozen=True)
class Query:
    id: str
    log: str  # a preserved log in evaluation/logs/; its run comes from manifest.json
    expected_category: CauseCategory  # used only when no verified team incident records this run
    seen_in_development: bool  # whether the log was available when the current rules were written


# Every failed run in scmbuildrelease/CIRCA up to 2026-11-01, plus the tt-metal log already in evaluation/.
# Whether a case is ground truth, and its expected category, come from team/incidents.json: only a verified
# incident recorded from the same run counts.
QUERIES = [
    Query("circa-39988949831-prettier", "circa-39988949831-prettier.log", "software", True),
    Query("circa-39988949831-ruff-format", "circa-39988949831-ruff-format.log", "software", True),
    Query("circa-39988949831-bandit", "circa-39988949831-bandit.log", "software", True),
    Query("circa-39988949831-mypy", "circa-39988949831-mypy.log", "software", True),
    Query("circa-41331905922-prettier-scratch-files", "circa-41331905922-prettier-scratch-files.log", "software", False),
    Query("circa-41345998382-zizmor-advisory", "circa-41345998382-zizmor-advisory.log", "software", False),
    Query("circa-39859422492-ruff-import-sort", "circa-39859422492-ruff-import-sort.log", "software", True),
    Query("circa-41312342881-pytest-assertion", "circa-41312342881-pytest-assertion.log", "software", True),
    Query("tt-metal-protected-tag", "tt-metal-protected-tag.log", "infrastructure", True),
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


def read_preserved(file_name: str, log_dir: Path = LOG_DIR) -> tuple[str, str]:
    """A preserved log and its source ("owner/repo#run"), after checking it against the manifest."""

    manifest = json.loads((log_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next((entry for entry in manifest if entry["file"] == file_name), None)
    if entry is None:
        raise ValueError(f"{file_name} is not in the preserved-log manifest")
    text = (log_dir / file_name).read_text(encoding="utf-8")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != entry["sha256"]:
        raise ValueError(f"{file_name} does not match its manifest checksum")
    return text, f"{entry['repository']}#{entry['run']}"


def recorded_from(incident: HistoricalIncident, source: str) -> bool:
    """Whether a team incident was recorded from this run: its environment or a fix reference names it."""

    repository, run = source.split("#")
    environment = incident.environment
    if environment.get("repository") == repository and environment.get("run") == run:
        return True
    references = incident.fix.references if incident.fix else []
    return any(f"github.com/{repository}/actions/runs/{run}" in reference for reference in references)


def evaluate(team: list[HistoricalIncident], log_dir: Path = LOG_DIR) -> list[QueryResult]:
    team_ids = {incident.id for incident in team}
    root_causes = {incident.root_cause: incident.id for incident in team if incident.root_cause}
    results: list[QueryResult] = []
    for query in QUERIES:
        raw_log, source = read_preserved(query.log, log_dir)
        own = [incident for incident in team if recorded_from(incident, source)]
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
                source=source,
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
    return results


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
    parser.add_argument("--json", action="store_true", help="Print per-case results and metrics as JSON.")
    args = parser.parse_args()

    before = TEAM_FILE.read_bytes()
    results = evaluate(load_knowledge(TEAM_FILE).incidents)
    assert TEAM_FILE.read_bytes() == before, "team knowledge must not change"
    summary = summarize(results)

    if args.json:
        print(json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=2))
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


if __name__ == "__main__":
    main()
