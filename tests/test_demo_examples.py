from pathlib import Path

from circa.pipeline import analyze_log


def test_documented_disk_full_demo_has_hardware_evidence() -> None:
    raw_log = (Path(__file__).parents[1] / "examples" / "demo_disk_full.log").read_text()

    result = analyze_log(raw_log)

    assert result.category == "hardware"
    assert result.owner == "hardware-ops"
    assert "synthetic-hardware-disk-001" in result.matched_incident_ids
    assert result.evidence


def test_documented_insufficient_evidence_demo_stays_unknown() -> None:
    raw_log = (Path(__file__).parents[1] / "examples" / "demo_insufficient_evidence.log").read_text()

    result = analyze_log(raw_log)

    assert result.category == "unknown"
    assert result.confidence == 0.0
    assert result.matched_incident_ids == []
