"""Local, deterministic search over historical CI incidents."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from .fingerprint import normalize_environment
from .models import CIFailure, HistoricalIncident, KeywordIncidentMatch

_TOKEN = re.compile(r"[a-z0-9_./-]+")
# Function words and generic failure verbs carry no cause information but appear
# in most failures and incident summaries, so they must not count as overlap.
_STOPWORDS = frozenset(
    {
        "a", "after", "an", "and", "are", "as", "at", "be", "because", "been", "by", "could", "error",
        "failed", "failure", "for", "from", "has", "in", "is", "it", "its", "of", "on", "or", "the",
        "this", "to", "was", "were", "when", "while", "with",
    }
)


def load_incidents(path: str | Path) -> list[HistoricalIncident]:
    """Load and validate historical incidents from a local JSON file."""

    records = json.loads(Path(path).read_text(encoding="utf-8"))
    return [HistoricalIncident.model_validate(record) for record in records]


def tokenize(text: str) -> set[str]:
    """Return meaningful lowercase tokens, keeping literal codes such as ``503``."""

    return set(_TOKEN.findall(text.lower())) - _STOPWORDS


def incident_tokens(incident: HistoricalIncident) -> set[str]:
    return tokenize(" ".join([incident.title, incident.summary, *incident.keywords]))


def _score(failure: CIFailure, incident: HistoricalIncident) -> float:
    overlap = tokenize(failure.failure_text) & incident_tokens(incident)
    if not overlap:
        return 0.0

    score = float(len(overlap))
    if failure.category == incident.category:
        score += 1.0

    failure_environment = dict(normalize_environment(failure.environment))
    incident_environment = dict(normalize_environment(incident.environment))
    score += sum(
        0.5
        for key, value in failure_environment.items()
        if incident_environment.get(key) == value
    )
    return score


def find_similar_incident_matches(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident],
    limit: int = 3,
) -> list[KeywordIncidentMatch]:
    """Return scored local matches using keyword, category, and environment overlap."""

    if limit < 1:
        return []

    scored = (
        (incident, _score(failure, incident))
        for incident in incidents
    )
    matches = [KeywordIncidentMatch(incident=incident, score=score) for incident, score in scored if score > 0]
    matches.sort(key=lambda match: (-match.score, match.incident.id))
    return matches[:limit]


def find_similar_incidents(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident],
    limit: int = 3,
) -> list[HistoricalIncident]:
    """Return the original unscored local keyword matches for compatibility."""

    return [match.incident for match in find_similar_incident_matches(failure, incidents, limit)]
