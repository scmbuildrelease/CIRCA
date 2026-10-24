import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from circa.analyzer import analyze_failure
from circa.knowledge import find_similar_incidents, load_incidents
from circa.knowledge_store import (
    KnowledgeError,
    add_incident,
    load_analysis_incidents,
    load_knowledge,
    save_knowledge,
    serialize_incidents,
    supersede_incident,
    update_incident,
    verify_incident,
)
from circa.models import CIFailure, HistoricalIncident
from circa.resources import data_path

TODAY = date(2026, 10, 24)
VALIDATED_FIX = {
    "description": "Re-enabled the scratch cleanup job.",
    "validation_status": "validated",
    "validation_method": "reproduced_then_fixed",
    "validation_evidence": "Reproduced the full volume, applied cleanup, and the build passed.",
    "references": ["change-1234"],
}


def _draft(identifier: str = "team-disk-001", **overrides: object) -> dict:
    draft = {
        "id": identifier,
        "title": "Scratch volume full on build workers",
        "category": "hardware",
        "summary": "The worker scratch volume filled up during linking.",
        "keywords": ["scratch", "volume", "linker"],
        "recommendation": "Prune stale build directories.",
        "synthetic": False,
        "evidence": ["2026-10-22T10:14:16Z [ERROR] linker output could not be written: no space left on device"],
        "root_cause": "The nightly scratch cleanup job was disabled.",
        "fix": VALIDATED_FIX,
    }
    return draft | overrides


def _incident(**overrides: object) -> HistoricalIncident:
    record = _draft() | {"status": "pending_review"} | overrides
    return HistoricalIncident.model_validate(record)


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps(records, default=str), encoding="utf-8")
    return path


def _saved(path: Path, *drafts: dict, verify: bool = False) -> Path:
    """Create a knowledge file through the real lifecycle functions."""

    for draft in drafts:
        knowledge = load_knowledge(path, must_exist=False)
        incidents, incident, _ = add_incident(knowledge, draft, allow_similar=True, today=TODAY)
        save_knowledge(knowledge, incidents)
        if verify:
            knowledge = load_knowledge(path)
            incidents, _ = verify_incident(knowledge, incident.id, reviewer="reviewer", today=TODAY)
            save_knowledge(knowledge, incidents)
    return path


# Schema validation


def test_packaged_synthetic_incidents_remain_valid_and_verified() -> None:
    incidents = load_incidents(data_path("incidents.json"))

    assert len(incidents) == 8
    assert all(incident.synthetic and incident.status == "verified" for incident in incidents)
    assert all(incident.fix is None and incident.fingerprints == [] for incident in incidents)


def test_real_incidents_are_accepted_as_pending_review() -> None:
    assert _incident(fix=None, root_cause=None).status == "pending_review"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"root_cause": None}, "requires a root_cause"),
        ({"fix": None}, "fix with validation_status 'validated'"),
        ({"fix": VALIDATED_FIX | {"validation_status": "unvalidated"}}, "validation_status 'validated'"),
        ({"reviewed_by": None}, "requires reviewed_by"),
    ],
)
def test_verified_real_incident_requires_complete_validation(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _incident(**({"status": "verified", "reviewed_by": "reviewer"} | overrides))


@pytest.mark.parametrize(
    "fix_overrides",
    [
        {"validation_method": None},
        {"validation_evidence": " "},
        {"validation_method": "single_retry_passed"},
        {"validation_method": "repeated_runs_passed", "validation_evidence": "Retried once and it passed."},
        {"validation_method": "repeated_runs_passed", "validation_evidence": "2 consecutive runs passed."},
    ],
)
def test_validated_fix_rejects_missing_or_weak_validation(fix_overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _incident(fix=VALIDATED_FIX | fix_overrides)


def test_repeated_runs_with_enough_runs_is_a_valid_method() -> None:
    fix = VALIDATED_FIX | {"validation_method": "repeated_runs_passed", "validation_evidence": "5 consecutive runs."}

    assert _incident(fix=fix).fix.validation_method == "repeated_runs_passed"


def test_superseded_status_and_superseded_by_must_be_set_together() -> None:
    with pytest.raises(ValidationError, match="superseded_by"):
        _incident(status="superseded")
    with pytest.raises(ValidationError, match="superseded_by"):
        _incident(superseded_by="other")
    with pytest.raises(ValidationError, match="supersede itself"):
        _incident(status="superseded", superseded_by="team-disk-001")


@pytest.mark.parametrize(
    "evidence",
    [
        ["line one\nline two"],
        ["x" * 301],
        [" "],
        [f"line {number}" for number in range(11)],
    ],
)
def test_evidence_must_be_short_single_lines(evidence: list[str]) -> None:
    with pytest.raises(ValidationError):
        _incident(evidence=evidence)


# Fake credentials are assembled at runtime so no secret-shaped literal is
# committed for the repository's Gitleaks scan to report.
@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "key " + "AKIA" + "ABCDEFGHIJKLMNOP" + " leaked",
        "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb" + "-1234567890-abcdefghij",
        "password=hunter2",
        "API_KEY: s3cr3t-value",
    ],
    ids=["private-key", "aws-key", "github-token", "slack-token", "password-assignment", "api-key-assignment"],
)
def test_secret_patterns_are_rejected_in_any_text_field(secret: str) -> None:
    with pytest.raises(ValidationError, match="secret"):
        _incident(evidence=[f"login failed: {secret}"])
    with pytest.raises(ValidationError, match="secret"):
        _incident(fix=VALIDATED_FIX | {"references": [secret]})
    with pytest.raises(ValidationError, match="secret"):
        _incident(environment={"note": secret})


@pytest.mark.parametrize("redacted", ["password=<redacted>", "token: ***", "token expired while fetching"])
def test_redacted_or_ordinary_wording_is_allowed(redacted: str) -> None:
    assert _incident(evidence=[f"auth failed: {redacted}"])


# Knowledge file loading


def test_team_records_must_set_an_explicit_status(tmp_path: Path) -> None:
    record = _draft()
    path = _write(tmp_path / "team.json", [record])

    with pytest.raises(KnowledgeError, match="explicit 'status'"):
        load_knowledge(path)


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([_draft() | {"status": "pending_review"}] * 2, "duplicate incident ID"),
        ([_draft("synthetic-hardware-disk-001") | {"status": "pending_review"}], "collides with a packaged incident"),
        (
            [_draft() | {"status": "superseded", "superseded_by": "missing", "reviewed_by": "r"}],
            "unknown incident 'missing'",
        ),
        (
            [
                _draft("a") | {"status": "pending_review", "fingerprints": ["f1"]},
                _draft("b") | {"status": "pending_review", "fingerprints": ["f1"]},
            ],
            "share a dedup fingerprint",
        ),
    ],
)
def test_cross_record_problems_are_rejected(tmp_path: Path, records: list[dict], message: str) -> None:
    with pytest.raises(KnowledgeError, match=message):
        load_knowledge(_write(tmp_path / "team.json", records))


@pytest.mark.parametrize("content", ["not json", '{"id": "x"}', '[{"status": "verified", "id": ""}]'])
def test_malformed_knowledge_files_raise_clear_errors(tmp_path: Path, content: str) -> None:
    path = tmp_path / "team.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(KnowledgeError):
        load_knowledge(path)


def test_missing_file_is_an_error_unless_creating(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeError, match="not found"):
        load_knowledge(tmp_path / "missing.json")

    assert load_knowledge(tmp_path / "missing.json", must_exist=False).incidents == []


# Lifecycle and status transitions


def test_add_always_starts_pending_review_with_fingerprint_and_dates(tmp_path: Path) -> None:
    knowledge = load_knowledge(tmp_path / "team.json", must_exist=False)

    _, incident, warnings = add_incident(knowledge, _draft(), today=TODAY)

    assert incident.status == "pending_review"
    assert len(incident.fingerprints) == 1
    assert incident.created == incident.updated == TODAY
    assert warnings == []


@pytest.mark.parametrize("field", ["status", "fingerprints", "reviewed_by", "superseded_by", "created", "updated"])
def test_drafts_cannot_set_managed_fields(tmp_path: Path, field: str) -> None:
    knowledge = load_knowledge(tmp_path / "team.json", must_exist=False)

    with pytest.raises(KnowledgeError, match="managed field"):
        add_incident(knowledge, _draft() | {field: "verified"}, today=TODAY)


def test_drafts_require_evidence_for_deduplication(tmp_path: Path) -> None:
    knowledge = load_knowledge(tmp_path / "team.json", must_exist=False)

    with pytest.raises(KnowledgeError, match="evidence"):
        add_incident(knowledge, _draft(evidence=[]), today=TODAY)


def test_add_rejects_existing_and_packaged_ids(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())

    with pytest.raises(KnowledgeError, match="already exists"):
        add_incident(load_knowledge(path), _draft(evidence=["other line"]), today=TODAY)
    with pytest.raises(KnowledgeError, match="already exists"):
        add_incident(load_knowledge(path), _draft("synthetic-hardware-disk-001", evidence=["x"]), today=TODAY)


def test_verify_moves_pending_to_verified_with_reviewer(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())

    _, incident = verify_incident(load_knowledge(path), "team-disk-001", reviewer="release-reviewer", today=TODAY)

    assert incident.status == "verified"
    assert incident.reviewed_by == "release-reviewer"


def test_verify_rejects_incomplete_real_incident(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft(fix=VALIDATED_FIX | {"validation_status": "unvalidated"}))

    with pytest.raises(KnowledgeError, match="cannot verify"):
        verify_incident(load_knowledge(path), "team-disk-001", reviewer="reviewer", today=TODAY)


def test_verify_requires_a_reviewer_and_a_pending_incident(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    with pytest.raises(KnowledgeError, match="reviewer"):
        verify_incident(load_knowledge(path), "team-disk-001", reviewer=" ", today=TODAY)

    verified = _saved(tmp_path / "verified.json", _draft(), verify=True)
    with pytest.raises(KnowledgeError, match="only pending_review"):
        verify_incident(load_knowledge(verified), "team-disk-001", reviewer="reviewer", today=TODAY)
    with pytest.raises(KnowledgeError, match="not found"):
        verify_incident(load_knowledge(verified), "missing", reviewer="reviewer", today=TODAY)


def test_update_revises_pending_incident_and_keeps_it_pending(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())

    incidents, incident, _ = update_incident(
        load_knowledge(path), _draft(summary="Revised summary."), allow_similar=True, today=date(2026, 11, 1)
    )

    assert incident.status == "pending_review"
    assert incident.summary == "Revised summary."
    assert incident.created == TODAY
    assert incident.updated == date(2026, 11, 1)
    assert len(incidents) == 1


def test_update_rejects_verified_incidents(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft(), verify=True)

    with pytest.raises(KnowledgeError, match="only pending_review incidents can be updated"):
        update_incident(load_knowledge(path), _draft(summary="Changed."), today=TODAY)


def test_supersede_requires_two_verified_incidents(tmp_path: Path) -> None:
    newer = _draft("team-disk-002", evidence=["new cleanup policy failure"], keywords=["policy"])
    path = _saved(tmp_path / "team.json", _draft(), newer, verify=True)

    _incidents, incident = supersede_incident(load_knowledge(path), "team-disk-001", by="team-disk-002", today=TODAY)

    assert incident.status == "superseded"
    assert incident.superseded_by == "team-disk-002"
    with pytest.raises(KnowledgeError, match="different, verified"):
        supersede_incident(load_knowledge(path), "team-disk-001", by="team-disk-001", today=TODAY)
    pending = _saved(tmp_path / "pending.json", _draft(), newer)
    with pytest.raises(KnowledgeError, match="only verified"):
        supersede_incident(load_knowledge(pending), "team-disk-001", by="team-disk-002", today=TODAY)


# Duplicate detection


def test_exact_duplicate_is_rejected_across_timestamps_and_ids(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    same_failure_later = _draft(
        "team-disk-099",
        title="Different title",
        keywords=[],
        evidence=["2026-11-02 08:00:01 [error] linker output could not be written: no space left on device"],
    )

    with pytest.raises(KnowledgeError, match="duplicate of existing incident 'team-disk-001'"):
        add_incident(load_knowledge(path), same_failure_later, allow_similar=True, today=TODAY)


def test_near_duplicate_requires_explicit_allowance_and_warns(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    similar = _draft("team-disk-002", evidence=["scratch volume exhausted during linker step"])

    with pytest.raises(KnowledgeError, match="possible duplicate of: team-disk-001"):
        add_incident(load_knowledge(path), similar, today=TODAY)
    _, _, warnings = add_incident(load_knowledge(path), similar, allow_similar=True, today=TODAY)

    assert warnings and "team-disk-001" in warnings[0]


def test_unrelated_incident_is_not_a_near_duplicate(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    unrelated = _draft(
        "team-dns-001",
        title="Resolver outage",
        category="network",
        summary="Internal DNS resolver stopped answering.",
        keywords=["dns", "resolver"],
        recommendation="Restart the resolver.",
        evidence=["could not resolve host artifacts.internal"],
    )

    _, _, warnings = add_incident(load_knowledge(path), unrelated, today=TODAY)

    assert warnings == []


# Safe writes


def test_save_is_atomic_when_the_write_fails(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    original = path.read_bytes()
    knowledge = load_knowledge(path)
    incidents, _ = verify_incident(knowledge, "team-disk-001", reviewer="reviewer", today=TODAY)

    with patch("circa.knowledge_store.os.replace", side_effect=OSError("disk full")), pytest.raises(OSError):
        save_knowledge(knowledge, incidents)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_save_refuses_when_file_changed_since_read(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft())
    knowledge = load_knowledge(path)
    incidents, _ = verify_incident(knowledge, "team-disk-001", reviewer="reviewer", today=TODAY)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(KnowledgeError, match="changed since it was read"):
        save_knowledge(knowledge, incidents)


def test_save_refuses_when_a_new_file_appeared_since_read(tmp_path: Path) -> None:
    path = tmp_path / "team.json"
    knowledge = load_knowledge(path, must_exist=False)
    incidents, _, _ = add_incident(knowledge, _draft(), today=TODAY)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(KnowledgeError, match="changed since it was read"):
        save_knowledge(knowledge, incidents)


def test_serialization_is_stable_and_round_trips(tmp_path: Path) -> None:
    path = _saved(tmp_path / "team.json", _draft("team-b", evidence=["b"]), _draft("team-a", evidence=["a"]))
    incidents = load_knowledge(path).incidents

    assert [record["id"] for record in json.loads(path.read_text(encoding="utf-8"))] == ["team-a", "team-b"]
    assert serialize_incidents(reversed(incidents)) == path.read_text(encoding="utf-8")
    assert "superseded_by" not in path.read_text(encoding="utf-8")


# Matching and backward compatibility


def test_only_verified_incidents_participate_in_matching(tmp_path: Path) -> None:
    failure = CIFailure(failure_text="linker output could not be written: scratch volume full")
    pending = _saved(tmp_path / "pending.json", _draft())
    verified = _saved(tmp_path / "verified.json", _draft(), verify=True)

    pending_ids = [item.id for item in find_similar_incidents(failure, load_analysis_incidents(pending), 10)]
    verified_ids = [item.id for item in find_similar_incidents(failure, load_analysis_incidents(verified), 10)]
    direct_ids = [item.id for item in find_similar_incidents(failure, load_knowledge(pending).incidents, 10)]

    assert "team-disk-001" not in pending_ids
    assert "team-disk-001" in verified_ids
    assert direct_ids == []


def test_superseded_incidents_do_not_participate_in_matching(tmp_path: Path) -> None:
    newer = _draft("team-disk-002", evidence=["new failure"], keywords=["policy"])
    path = _saved(tmp_path / "team.json", _draft(), newer, verify=True)
    knowledge = load_knowledge(path)
    incidents, _ = supersede_incident(knowledge, "team-disk-001", by="team-disk-002", today=TODAY)
    save_knowledge(knowledge, incidents)

    assert "team-disk-001" not in {item.id for item in load_analysis_incidents(path)}


def test_verified_team_root_cause_is_reported_and_packaged_results_are_unchanged(tmp_path: Path) -> None:
    failure = CIFailure(failure_text="scratch volume full during the linker step on build workers")
    path = _saved(tmp_path / "team.json", _draft(), verify=True)

    baseline = analyze_failure(failure)
    explicit_packaged = analyze_failure(failure, load_analysis_incidents(None))
    with_team = analyze_failure(failure, load_analysis_incidents(path))

    assert explicit_packaged == baseline
    assert with_team.matched_incident_ids[0] == "team-disk-001"
    assert with_team.root_cause == "The nightly scratch cleanup job was disabled."
