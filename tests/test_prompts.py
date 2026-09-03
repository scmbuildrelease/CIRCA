from circa.models import AnalysisResult, CIFailure
from circa.prompts import build_classification_prompt, build_explanation_prompt


def test_prompt_contains_only_supplied_analysis_context() -> None:
    result = AnalysisResult(
        fingerprint="abc",
        category="hardware",
        root_cause="Disk is full.",
        recommendation="Free disk space.",
        evidence=["disk matched"],
        matched_incident_ids=["synthetic-hardware-disk-001"],
    )

    prompt = build_explanation_prompt("ERROR: no space left", result)

    assert "ERROR: no space left" in prompt
    assert "hardware" in prompt
    assert "synthetic-hardware-disk-001" in prompt
    assert "must not be changed" in prompt


def test_classification_prompt_uses_only_bounded_failure_context() -> None:
    result = AnalysisResult(
        fingerprint="abc",
        category="hardware",
        root_cause="Disk is full.",
        recommendation="Free disk space.",
        matched_incident_ids=["synthetic-hardware-disk-001"],
    )

    prompt = build_classification_prompt(
        CIFailure(failure_text="ERROR: no space left", environment={"os": "linux"}), result
    )

    assert "ERROR: no space left" in prompt
    assert "os=linux" in prompt
    assert "synthetic-hardware-disk-001" in prompt
    assert "unknown" in prompt
    assert "Return JSON only" in prompt
