"""Human-reviewed lifecycle for a team's local historical-incident knowledge file.

The packaged synthetic incidents are read-only. Team knowledge lives in a
separate, explicitly supplied JSON file. Nothing in ordinary analysis writes it;
only the ``circa knowledge`` commands do, after explicit confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .fingerprint import dedup_fingerprint
from .knowledge import find_similar_incident_matches, load_incidents
from .models import CIFailure, HistoricalIncident, IncidentFix, KeywordIncidentMatch
from .resources import data_path

# Keyword-overlap score (see knowledge._score) at or above which an incident is
# reported as a near duplicate. Several shared distinctive terms are required.
NEAR_DUPLICATE_MIN_SCORE = 4.0
# Fields a draft may not set: they are computed or changed only by lifecycle commands.
MANAGED_FIELDS = frozenset({"status", "fingerprints", "superseded_by", "reviewed_by", "created", "updated"})


class KnowledgeError(ValueError):
    """An invalid knowledge file, draft, or lifecycle transition."""


class DuplicateIncidentError(KnowledgeError):
    """A draft duplicates, or nearly duplicates, an existing team incident."""


@dataclass(frozen=True)
class KnowledgeFile:
    """Validated team incidents plus the digest of the bytes they were read from."""

    path: Path
    incidents: list[HistoricalIncident]
    digest: str | None  # ``None`` when the file does not exist yet.


def _today() -> date:
    """Lifecycle dates are recorded in UTC so they do not depend on the reviewer's timezone."""

    return datetime.now(UTC).date()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _packaged_ids() -> set[str]:
    return {incident.id for incident in load_incidents(data_path("incidents.json"))}


def _validation_message(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, item['loc'])) or 'record'}: {item['msg']}" for item in error.errors())


def check_incidents(incidents: Iterable[HistoricalIncident]) -> None:
    """Raise ``KnowledgeError`` for cross-record problems in one team file."""

    records = list(incidents)
    errors: list[str] = []
    ids = [incident.id for incident in records]
    packaged = _packaged_ids()
    errors += [f"duplicate incident ID '{item}'" for item in sorted({i for i in ids if ids.count(i) > 1})]
    errors += [f"incident ID '{item}' collides with a packaged incident" for item in sorted(set(ids) & packaged)]
    owners: dict[str, str] = {}
    for incident in records:
        for fingerprint in incident.fingerprints:
            if fingerprint in owners and owners[fingerprint] != incident.id:
                errors.append(f"incidents '{owners[fingerprint]}' and '{incident.id}' share a dedup fingerprint")
            owners.setdefault(fingerprint, incident.id)
        if incident.superseded_by is not None and incident.superseded_by not in ids:
            errors.append(f"incident '{incident.id}' is superseded by unknown incident '{incident.superseded_by}'")
    if errors:
        raise KnowledgeError("; ".join(errors))


def load_knowledge(path: str | Path, *, must_exist: bool = True) -> KnowledgeFile:
    """Load and fully validate a team knowledge file."""

    source = Path(path)
    if not source.exists():
        if must_exist:
            raise KnowledgeError(f"knowledge file not found: {source}")
        return KnowledgeFile(source, [], None)
    data = source.read_bytes()
    try:
        records = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgeError(f"{source} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(records, list):
        raise KnowledgeError(f"{source} must contain a JSON list of incidents")
    incidents: list[HistoricalIncident] = []
    for index, record in enumerate(records):
        # The model defaults status to "verified" for packaged data; team records
        # must state it so an omitted field can never skip review.
        if not isinstance(record, dict) or "status" not in record:
            raise KnowledgeError(f"{source} record {index}: every team incident must set an explicit 'status'")
        try:
            incidents.append(HistoricalIncident.model_validate(record))
        except ValidationError as error:
            raise KnowledgeError(f"{source} record {index}: {_validation_message(error)}") from error
    check_incidents(incidents)
    return KnowledgeFile(source, incidents, _digest(data))


def load_analysis_incidents(knowledge_path: str | Path | None = None) -> list[HistoricalIncident]:
    """Return packaged incidents plus, if supplied, the team file's verified incidents."""

    team = load_knowledge(knowledge_path).incidents if knowledge_path is not None else []
    return analysis_incidents(team)


def analysis_incidents(team_incidents: Iterable[HistoricalIncident]) -> list[HistoricalIncident]:
    """Return packaged incidents plus the verified subset of already loaded team incidents."""

    return load_incidents(data_path("incidents.json")) + [item for item in team_incidents if item.status == "verified"]


def find_near_duplicates(probe: CIFailure, incidents: Iterable[HistoricalIncident]) -> list[KeywordIncidentMatch]:
    """Return team incidents of any status that share enough distinctive terms with ``probe``."""

    matches = find_similar_incident_matches(probe, incidents, limit=5, include_unverified=True)
    return [match for match in matches if match.score >= NEAR_DUPLICATE_MIN_SCORE]


def _incident_from_draft(draft: Mapping[str, Any], *, created: date | None, today: date) -> HistoricalIncident:
    managed = sorted(MANAGED_FIELDS & set(draft))
    if managed:
        raise KnowledgeError(f"drafts may not set managed field(s): {', '.join(managed)}")
    if not draft.get("evidence"):
        raise KnowledgeError("drafts require at least one sanitized evidence line for deduplication")
    record = {**draft, "status": "pending_review", "created": created or today, "updated": today}
    try:
        incident = HistoricalIncident.model_validate(record)
    except ValidationError as error:
        raise KnowledgeError(f"invalid draft: {_validation_message(error)}") from error
    return incident.model_copy(update={"fingerprints": [dedup_fingerprint(incident.evidence)]})


def _check_duplicates(incident: HistoricalIncident, others: list[HistoricalIncident], allow_similar: bool) -> list[str]:
    """Reject exact duplicates; return near-duplicate warnings or reject them unless allowed."""

    fingerprint = incident.fingerprints[0]
    exact = [other.id for other in others if fingerprint in other.fingerprints]
    if exact:
        raise DuplicateIncidentError(f"duplicate of existing incident '{exact[0]}' (same dedup fingerprint)")
    probe = CIFailure(
        failure_text=" ".join([incident.title, incident.summary, *incident.keywords, *incident.evidence]),
        environment=incident.environment,
        category=incident.category,
    )
    # Pending and superseded knowledge is compared too, so a fix is not re-added unknowingly.
    similar = [f"{match.incident.id} (keyword score {match.score:g})" for match in find_near_duplicates(probe, others)]
    if similar and not allow_similar:
        raise DuplicateIncidentError(
            f"possible duplicate of: {', '.join(similar)}; update that incident, or pass --allow-similar if distinct"
        )
    return [f"similar to {item}" for item in similar]


def add_incident(
    knowledge: KnowledgeFile, draft: Mapping[str, Any], *, allow_similar: bool = False, today: date | None = None
) -> tuple[list[HistoricalIncident], HistoricalIncident, list[str]]:
    """Return the updated incident list with a new ``pending_review`` incident."""

    incident = _incident_from_draft(draft, created=None, today=today or _today())
    if any(other.id == incident.id for other in knowledge.incidents) or incident.id in _packaged_ids():
        raise KnowledgeError(f"incident ID '{incident.id}' already exists; use 'circa knowledge update'")
    warnings = _check_duplicates(incident, knowledge.incidents, allow_similar)
    return [*knowledge.incidents, incident], incident, warnings


def update_incident(
    knowledge: KnowledgeFile, draft: Mapping[str, Any], *, allow_similar: bool = False, today: date | None = None
) -> tuple[list[HistoricalIncident], HistoricalIncident, list[str]]:
    """Replace a ``pending_review`` incident's content; it stays pending review."""

    existing = _find(knowledge, str(draft.get("id", "")))
    if existing.status != "pending_review":
        raise KnowledgeError(
            f"incident '{existing.id}' is {existing.status}; only pending_review incidents can be updated. "
            "Add a new incident and supersede this one instead."
        )
    incident = _incident_from_draft(draft, created=existing.created, today=today or _today())
    others = [other for other in knowledge.incidents if other.id != existing.id]
    warnings = _check_duplicates(incident, others, allow_similar)
    return _replace(knowledge, incident), incident, warnings


def verify_incident(
    knowledge: KnowledgeFile,
    incident_id: str,
    *,
    reviewer: str,
    validation: tuple[str, str] | None = None,
    today: date | None = None,
) -> tuple[list[HistoricalIncident], HistoricalIncident]:
    """Move a ``pending_review`` incident to ``verified`` if it meets the schema rules.

    ``validation`` is the reviewer's own ``(method, evidence)`` for the existing
    fix. Approval alone never marks a fix validated.
    """

    existing = _find(knowledge, incident_id)
    if existing.status != "pending_review":
        raise KnowledgeError(f"incident '{incident_id}' is {existing.status}; only pending_review can be verified")
    if not reviewer.strip():
        raise KnowledgeError("a reviewer name is required")
    record = existing.model_dump() | {"status": "verified", "reviewed_by": reviewer, "updated": today or _today()}
    try:
        if validation is not None:
            if existing.fix is None:
                raise KnowledgeError(f"incident '{incident_id}' has no fix to validate; add one with an update")
            method, evidence = validation
            fix = existing.fix.model_dump() | {
                "validation_status": "validated",
                "validation_method": method,
                "validation_evidence": evidence,
            }
            record["fix"] = IncidentFix.model_validate(fix)
        incident = HistoricalIncident.model_validate(record)
    except ValidationError as error:
        raise KnowledgeError(f"cannot verify '{incident_id}': {_validation_message(error)}") from error
    return _replace(knowledge, incident), incident


def reject_incident(
    knowledge: KnowledgeFile, incident_id: str, *, reviewer: str, today: date | None = None
) -> tuple[list[HistoricalIncident], HistoricalIncident]:
    """Archive a ``pending_review`` incident as ``rejected``; it is kept but never matched."""

    existing = _find(knowledge, incident_id)
    if existing.status != "pending_review":
        raise KnowledgeError(f"incident '{incident_id}' is {existing.status}; only pending_review can be rejected")
    if not reviewer.strip():
        raise KnowledgeError("a reviewer name is required")
    incident = existing.model_copy(update={"status": "rejected", "reviewed_by": reviewer, "updated": today or _today()})
    return _replace(knowledge, incident), incident


def supersede_incident(
    knowledge: KnowledgeFile, incident_id: str, *, by: str, today: date | None = None
) -> tuple[list[HistoricalIncident], HistoricalIncident]:
    """Mark a verified incident ``superseded`` by another verified incident."""

    existing = _find(knowledge, incident_id)
    replacement = _find(knowledge, by)
    if existing.status != "verified":
        raise KnowledgeError(f"incident '{incident_id}' is {existing.status}; only verified can be superseded")
    if replacement.status != "verified" or replacement.id == existing.id:
        raise KnowledgeError(f"replacement '{by}' must be a different, verified incident")
    incident = existing.model_copy(update={"status": "superseded", "superseded_by": by, "updated": today or _today()})
    return _replace(knowledge, incident), incident


def _find(knowledge: KnowledgeFile, incident_id: str) -> HistoricalIncident:
    match = next((incident for incident in knowledge.incidents if incident.id == incident_id), None)
    if match is None:
        raise KnowledgeError(f"incident '{incident_id}' not found in {knowledge.path}")
    return match


def _replace(knowledge: KnowledgeFile, incident: HistoricalIncident) -> list[HistoricalIncident]:
    return [incident if other.id == incident.id else other for other in knowledge.incidents]


def serialize_incidents(incidents: Iterable[HistoricalIncident]) -> str:
    """Return stable JSON: incidents sorted by ID, unset optional fields omitted."""

    records = [incident.model_dump(mode="json", exclude_none=True) for incident in incidents]
    return json.dumps(sorted(records, key=lambda record: record["id"]), indent=2, ensure_ascii=False) + "\n"


def save_knowledge(knowledge: KnowledgeFile, incidents: list[HistoricalIncident]) -> None:
    """Atomically write ``incidents``, refusing if the file changed since it was loaded."""

    check_incidents(incidents)
    path = knowledge.path
    current = _digest(path.read_bytes()) if path.exists() else None
    if current != knowledge.digest:
        raise KnowledgeError(f"{path} changed since it was read; reload and retry")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialize_incidents(incidents))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
