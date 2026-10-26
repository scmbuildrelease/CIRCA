from circa.models import AnalysisResult, CIFailure
from circa.prompts import (
    MAX_PROMPT_LOG_CHARS,
    build_classification_prompt,
    build_diagnosis_prompt,
    build_explanation_prompt,
)


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


def test_diagnosis_prompt_is_bounded_and_omits_empty_sections() -> None:
    failure = CIFailure(failure_text="\n".join(f"ERROR line {index} " + "x" * 80 for index in range(200)))
    result = AnalysisResult(fingerprint="f", category="unknown", root_cause="Unknown.", recommendation="Collect logs.")

    prompt = build_diagnosis_prompt(failure, result, [])

    assert len(prompt) < MAX_PROMPT_LOG_CHARS + 1000
    assert "ERROR line 0 " in prompt and "ERROR line 199 " not in prompt
    assert "Environment:" not in prompt and "Weak historical matches" not in prompt
