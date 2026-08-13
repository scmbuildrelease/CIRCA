from pathlib import Path

from circa.log_parser import extract_failure_text
from circa.pipeline import analyze_log


def test_raw_log_pipeline_extracts_and_analyzes_hardware_failure() -> None:
    raw_log = (Path(__file__).parents[1] / "examples" / "hardware_failure.log").read_text()

    extracted = extract_failure_text(raw_log)
    result = analyze_log(raw_log, environment={"os": "linux", "runner": "bare-metal"})

    assert "no space left on device" in extracted
    assert result.category == "hardware"
    assert result.owner == "hardware-ops"
    assert result.matched_incident_ids[0] == "synthetic-hardware-disk-001"


def test_raw_log_pipeline_returns_unknown_when_no_failure_is_recognized() -> None:
    result = analyze_log("build started\nall tasks complete\nexit code 0")

    assert result.category == "unknown"
    assert result.owner == "unknown"
