from pathlib import Path

from circa.analyzer import analyze_failure
from circa.fingerprint import failure_fingerprint
from circa.knowledge import load_incidents
from circa.models import CIFailure


def test_analyzer_uses_historical_match_for_network_failure() -> None:
    incidents = load_incidents(Path(__file__).parents[1] / "data" / "incidents.json")
    failure = CIFailure(
        failure_text="Dependency installation hit a package registry connection timeout",
        environment={"network": "external"},
    )

    result = analyze_failure(failure, incidents)

    assert result.category == "network"
    assert result.owner == "network-ops"
    assert result.confidence > 0.5
    assert result.matched_incident_ids == ["synthetic-network-timeout-005"]
    assert result.fingerprint == failure_fingerprint(failure)
    assert result.evidence


def test_analyzer_returns_unknown_for_weak_evidence() -> None:
    result = analyze_failure(CIFailure(failure_text="Job ended unexpectedly"), [])

    assert result.category == "unknown"
    assert result.owner == "unknown"
    assert result.confidence == 0.0
    assert result.matched_incident_ids == []
