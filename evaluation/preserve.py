"""Preserve a sanitized copy of a real CI failure log for evaluation.

GitHub deletes Actions logs after its retention period (90 days by default), so the
holdout evaluation reads preserved copies from evaluation/logs/ instead. Each copy has
colors removed and credentials redacted with CIRCA's own redaction, holds one job, and
is described in evaluation/logs/manifest.json: its run, job, capture time, incident,
evidence lines, and checksum. Nothing is written unless every evidence line of the
incident is found in the sanitized log.

Usage (from the repository root):

    python evaluation/preserve.py RUN_URL --incident ID --name SLUG [--job NAME] [--from-file LOG]

Without --from-file the log is fetched with the GitHub CLI (gh run view --log-failed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from circa.fingerprint import evidence_observed, normalized_log_lines
from circa.github_runs import fetch_failed_log, parse_run_reference
from circa.knowledge_store import load_knowledge
from circa.models import HistoricalIncident
from circa.redaction import contains_secret, redact_secrets

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "evaluation" / "logs"
MANIFEST = LOG_DIR / "manifest.json"
TEAM_FILE = ROOT / "team" / "incidents.json"
DRAFT_DIR = ROOT / "team" / "drafts"
# The same color codes circa.github_runs removes from logs it fetches.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest(path: Path = MANIFEST) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def find_incident(incident_id: str) -> HistoricalIncident:
    """The team incident (any status) or reviewable draft with this ID."""

    for incident in load_knowledge(TEAM_FILE).incidents:
        if incident.id == incident_id:
            return incident
    for draft in sorted(DRAFT_DIR.glob("*.json")):
        record = json.loads(draft.read_text(encoding="utf-8"))
        if record.get("id") == incident_id:
            return HistoricalIncident.model_validate({"status": "pending_review"} | record)  # drafts are unreviewed
    raise SystemExit(f"incident {incident_id} is not in {TEAM_FILE.name} or {DRAFT_DIR.name}/")


def sanitize(log: str, job: str | None) -> tuple[str, str | None]:
    """Remove colors, keep one job's lines, and redact credentials. Returns the text and its job."""

    lines = _ANSI.sub("", log).splitlines(keepends=True)
    jobs = {line.split("\t", 1)[0] for line in lines if "\t" in line}
    if job is not None:
        lines = [line for line in lines if line.split("\t", 1)[0] == job]
        if not lines:
            raise SystemExit(f"the log has no lines from job '{job}'")
    elif len(jobs) > 1:
        raise SystemExit(f"the log has {len(jobs)} jobs; choose one with --job")
    else:
        job = next(iter(jobs), None)
    text = redact_secrets("".join(lines))
    assert not contains_secret(text)  # redaction is idempotent; a hit here would be a redaction defect
    return text, job


def preserve(
    log: str,
    *,
    repository: str,
    run: str,
    incident: HistoricalIncident,
    name: str,
    job: str | None = None,
    log_dir: Path = LOG_DIR,
    captured_at: datetime | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Write the sanitized log and its manifest entry; refuses to overwrite either.

    ``note`` says how the log differs from a full ``--log-failed`` export, for example an excerpt.
    """

    text, job = sanitize(log, job)
    if not evidence_observed(incident.evidence, normalized_log_lines(text)):
        raise SystemExit(f"not every evidence line of {incident.id} is in the sanitized log; wrong run or job?")
    file_name = f"{repository.split('/')[1].lower()}-{run}-{name}.log"
    path = log_dir / file_name
    manifest_path = log_dir / MANIFEST.name
    manifest = load_manifest(manifest_path)
    if path.exists() or any(entry["file"] == file_name for entry in manifest):
        raise SystemExit(f"{file_name} is already preserved")
    entry = {
        "file": file_name,
        "repository": repository,
        "run": run,
        "job": job,
        "captured_at": (captured_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "incident": incident.id,
        "evidence": [redact_secrets(line) for line in incident.evidence],
        "sha256": sha256(text),
    }
    if note:
        entry["note"] = redact_secrets(note)
    path.write_text(text, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(sorted([*manifest, entry], key=lambda e: e["file"]), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", help="https://github.com/OWNER/REPO/actions/runs/RUN_ID")
    parser.add_argument("--incident", required=True, help="Team incident or draft ID whose evidence the log holds.")
    parser.add_argument("--name", required=True, help="Short slug for the file name, such as ruff-import-sort.")
    parser.add_argument("--job", help="Job to keep when the run has several failed jobs.")
    parser.add_argument("--from-file", type=Path, help="A log already fetched with 'gh run view --log-failed'.")
    parser.add_argument("--note", help="How the log differs from a full export, e.g. which lines an excerpt keeps.")
    args = parser.parse_args()

    run = parse_run_reference(args.run)
    raw = args.from_file.read_text(encoding="utf-8") if args.from_file else fetch_failed_log(run)
    entry = preserve(
        raw,
        repository=f"{run.owner}/{run.repo}",
        run=run.run_id,
        incident=find_incident(args.incident),
        name=args.name,
        job=args.job,
        note=args.note,
    )
    print(f"preserved {entry['file']} ({entry['job']}); review it before committing")


if __name__ == "__main__":
    main()
