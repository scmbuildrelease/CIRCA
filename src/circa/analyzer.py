"""Deterministic local CI failure analysis."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .fingerprint import failure_fingerprint, normalize_environment, normalize_failure_text
from .knowledge import find_similar_incidents, load_incidents
from .models import AnalysisResult, CIFailure, CauseCategory, HistoricalIncident
from .resources import data_path

_TOKEN = re.compile(r"[a-z0-9_./-]+")
_OWNERS = {
    "hardware": "hardware-ops",
    "network": "network-ops",
    "infrastructure": "ci-ops",
    "software": "developer",
    "unknown": "unknown",
}
_CATEGORY_KEYWORDS: dict[CauseCategory, set[str]] = {
    "hardware": {"disk", "memory", "oom", "filesystem", "cpu"},
    "software": {"assertion", "assertionerror", "modulenotfounderror", "import", "regression", "traceback"},
    "network": {"timeout", "dns", "resolve", "connection", "hostname", "registry"},
    "infrastructure": {"runner", "image", "provisioning", "artifact", "503", "unavailable"},
    "unknown": set(),
}


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(normalize_failure_text(text)))


def _incident_tokens(incident: HistoricalIncident) -> set[str]:
    return _tokens(" ".join([incident.title, incident.summary, *incident.keywords]))


def _environment_matches(failure: CIFailure, incident: HistoricalIncident) -> int:
    failure_environment = dict(normalize_environment(failure.environment))
    incident_environment = dict(normalize_environment(incident.environment))
    return sum(incident_environment.get(key) == value for key, value in failure_environment.items())


def _data_path() -> Path:
    return data_path("incidents.json")


def analyze_failure(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident] | None = None,
) -> AnalysisResult:
    """Analyze one failure using local keyword and environment evidence only.

    A category requires at least two matching incident keywords, or two direct
    category keywords when no incident is available. This favors ``unknown``
    when the evidence is sparse.
    """

    available_incidents = list(incidents) if incidents is not None else load_incidents(_data_path())
    matches = find_similar_incidents(failure, available_incidents)
    failure_tokens = _tokens(failure.failure_text)

    category_scores: dict[CauseCategory, float] = defaultdict(float)
    incident_overlap: dict[str, int] = {}
    environment_overlap: dict[str, int] = {}
    for incident in matches:
        keyword_overlap = len(failure_tokens & _incident_tokens(incident))
        environment_matches = _environment_matches(failure, incident)
        incident_overlap[incident.id] = keyword_overlap
        environment_overlap[incident.id] = environment_matches
        category_scores[incident.category] += keyword_overlap + (0.5 * environment_matches)

    direct_signals = {
        category: failure_tokens & keywords
        for category, keywords in _CATEGORY_KEYWORDS.items()
        if category != "unknown"
    }
    for category, signals in direct_signals.items():
        category_scores[category] += 0.25 * len(signals)

    candidate = max(
        (category for category in _CATEGORY_KEYWORDS if category != "unknown"),
        key=lambda category: category_scores[category],
    )
    strongest_match = next((match for match in matches if match.category == candidate), None)
    strong_incident_evidence = strongest_match is not None and incident_overlap[strongest_match.id] >= 2
    strong_direct_evidence = len(direct_signals[candidate]) >= 2

    if not (strong_incident_evidence or strong_direct_evidence):
        return AnalysisResult(
            fingerprint=failure_fingerprint(failure),
            category="unknown",
            confidence=0.0,
            root_cause="Insufficient deterministic evidence to identify a root cause.",
            evidence=[],
            recommendation="Collect additional failure output and environment details, then retry analysis.",
            owner=_OWNERS["unknown"],
            matched_incident_ids=[],
        )

    evidence: list[str] = []
    for incident in matches:
        if incident.category != candidate:
            continue
        evidence.append(
            f"Historical incident {incident.id} shares {incident_overlap[incident.id]} keyword(s)"
            f" and {environment_overlap[incident.id]} environment value(s)."
        )
    if direct_signals[candidate]:
        evidence.append(f"Failure text contains {', '.join(sorted(direct_signals[candidate]))}.")

    confidence = min(0.95, 0.35 + (0.1 * category_scores[candidate]))
    if strongest_match is not None:
        root_cause = strongest_match.summary
        recommendation = strongest_match.recommendation
    else:
        root_cause = f"Failure text indicates a likely {candidate} issue."
        recommendation = f"Investigate the {candidate} condition reported in the failure output."

    relevant_matches = [match.id for match in matches if match.category == candidate]
    return AnalysisResult(
        fingerprint=failure_fingerprint(failure),
        category=candidate,
        confidence=confidence,
        root_cause=root_cause,
        evidence=evidence,
        recommendation=recommendation,
        owner=_OWNERS[candidate],
        matched_incident_ids=relevant_matches,
    )
