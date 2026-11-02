import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from circa.fingerprint import evidence_observed, normalized_log_lines
from circa.models import HistoricalIncident
from circa.redaction import contains_secret

ROOT = Path(__file__).parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "evaluation" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look up their module while it is executing
    spec.loader.exec_module(module)
    return module


preserve = _load("preserve")
holdout = _load("holdout")

MANIFEST = json.loads((ROOT / "evaluation" / "logs" / "manifest.json").read_text(encoding="utf-8"))
TOKEN = "ghp_" + "c" * 36  # synthetic; matches the GitHub token pattern only
JOB = "Lint (Ruff)"
EVIDENCE = "tests/test_example.py:1:1: I001 [*] Import block is un-sorted or un-formatted"


def _incident() -> HistoricalIncident:
    return HistoricalIncident.model_validate(
        {
            "id": "real-example-001",
            "title": "Example",
            "category": "software",
            "summary": "Example",
            "keywords": ["ruff"],
            "status": "pending_review",
            "synthetic": False,
            "environment": {"repository": "example/repo", "run": "123"},
            "evidence": [EVIDENCE],
            "root_cause": "Example root cause.",
            "recommendation": "Example recommendation.",
        }
    )


def _raw_log() -> str:
    return (
        f"{JOB}\tRun ruff\t2026-11-01T10:00:00.0000000Z \x1b[31m{EVIDENCE}\x1b[0m\n"
        f"{JOB}\tRun ruff\t2026-11-01T10:00:00.1000000Z GH_TOKEN={TOKEN}\n"
        f"{JOB}\tRun ruff\t2026-11-01T10:00:00.2000000Z cloning https://deploy:pa@ss@example.com/repo.git\n"
        f"Tests\tRun pytest\t2026-11-01T10:00:01.0000000Z unrelated job line\n"
    )


def test_every_preserved_log_matches_its_manifest_entry_and_contains_no_credentials() -> None:
    files = sorted(path.name for path in (ROOT / "evaluation" / "logs").glob("*.log"))

    assert [entry["file"] for entry in MANIFEST] == files
    for entry in MANIFEST:
        text, source = holdout.read_preserved(entry["file"])  # checks the checksum
        assert source == f"{entry['repository']}#{entry['run']}"
        assert not contains_secret(text) and "\x1b" not in text
        assert entry["captured_at"] and entry["incident"] and entry["evidence"]
        assert evidence_observed(entry["evidence"], normalized_log_lines(text))
        jobs = {line.split("\t", 1)[0] for line in text.splitlines() if "\t" in line}
        assert jobs <= {entry["job"]}


def test_every_preserved_log_belongs_to_the_run_its_incident_records() -> None:
    for entry in MANIFEST:
        incident = preserve.find_incident(entry["incident"])

        assert holdout.recorded_from(incident, f"{entry['repository']}#{entry['run']}"), entry["file"]
        assert set(entry["evidence"]) <= set(incident.evidence)


def test_preserving_a_log_removes_colors_and_other_jobs_and_redacts_credentials(tmp_path: Path) -> None:
    captured = datetime(2026, 11, 2, tzinfo=UTC)

    entry = preserve.preserve(
        _raw_log(), repository="example/repo", run="123", incident=_incident(), name="ruff", job=JOB,
        log_dir=tmp_path, captured_at=captured,
    )  # fmt: skip

    text = (tmp_path / "repo-123-ruff.log").read_text(encoding="utf-8")
    assert TOKEN not in text and "pa@ss" not in text and "[REDACTED]" in text
    assert "\x1b" not in text and "unrelated job line" not in text
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == [entry]
    assert entry == {
        "file": "repo-123-ruff.log",
        "repository": "example/repo",
        "run": "123",
        "job": JOB,
        "captured_at": "2026-11-02T00:00:00+00:00",
        "incident": "real-example-001",
        "evidence": [EVIDENCE],
        "sha256": preserve.sha256(text),
    }
    # The holdout reads it back locally, with its run.
    assert holdout.read_preserved("repo-123-ruff.log", tmp_path) == (text, "example/repo#123")


def test_an_excerpt_is_labeled_in_the_manifest(tmp_path: Path) -> None:
    entry = preserve.preserve(_raw_log(), repository="example/repo", run="123", incident=_incident(), name="ruff",
                              job=JOB, log_dir=tmp_path, note="Excerpt: the error block only.")  # fmt: skip

    assert entry["note"] == "Excerpt: the error block only."
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))[0]["note"] == entry["note"]


def test_a_preserved_log_that_was_changed_or_not_listed_is_refused(tmp_path: Path) -> None:
    preserve.preserve(_raw_log(), repository="example/repo", run="123", incident=_incident(), name="ruff", job=JOB,
                      log_dir=tmp_path)  # fmt: skip
    (tmp_path / "unlisted.log").write_text("x", encoding="utf-8")
    path = tmp_path / "repo-123-ruff.log"
    path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        holdout.read_preserved("repo-123-ruff.log", tmp_path)
    with pytest.raises(ValueError, match="not in the preserved-log manifest"):
        holdout.read_preserved("unlisted.log", tmp_path)


def test_preservation_refuses_wrong_jobs_missing_evidence_and_overwrites(tmp_path: Path) -> None:
    def run(log: str, job: str | None) -> None:
        preserve.preserve(log, repository="example/repo", run="123", incident=_incident(), name="ruff", job=job,
                          log_dir=tmp_path)  # fmt: skip

    with pytest.raises(SystemExit, match="2 jobs; choose one"):
        run(_raw_log(), None)
    with pytest.raises(SystemExit, match="not every evidence line"):
        run(_raw_log(), "Tests")
    with pytest.raises(SystemExit, match="no lines from job"):
        run(_raw_log(), "Build")
    assert not list(tmp_path.iterdir())  # nothing is written when preservation is refused
    run(_raw_log(), JOB)
    with pytest.raises(SystemExit, match="already preserved"):
        run(_raw_log(), JOB)


def test_the_holdout_reads_every_case_from_a_preserved_log() -> None:
    preserved = {entry["file"] for entry in MANIFEST}

    assert {query.log for query in holdout.QUERIES} <= preserved
