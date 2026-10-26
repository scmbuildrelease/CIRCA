import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from circa.llm import OllamaProvider, classify_failure, explain_analysis
from circa.models import AnalysisResult, CIFailure, LLMClassification


def _result() -> AnalysisResult:
    return AnalysisResult(
        fingerprint="abc",
        category="hardware",
        root_cause="Disk is full.",
        recommendation="Free disk space.",
    )


def test_ollama_provider_returns_response() -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({"response": "Disk space caused the failure."}).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response) as mocked_urlopen:
        explanation = OllamaProvider(model="test-model", url="http://ollama.test").explain("prompt")

    assert explanation == "Disk space caused the failure."
    request = mocked_urlopen.call_args.args[0]
    assert request.full_url == "http://ollama.test/api/generate"
    assert json.loads(request.data) == {
        "model": "test-model",
        "prompt": "prompt",
        "stream": False,
        "options": {"temperature": 0},
    }


def test_ollama_provider_fails_gracefully() -> None:
    with patch("circa.llm.urlopen", side_effect=OSError("Ollama unavailable")):
        assert OllamaProvider().explain("prompt") is None


def test_ollama_provider_reads_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CIRCA_LLM_MODEL", "local-test")
    monkeypatch.setenv("CIRCA_OLLAMA_URL", "http://localhost:9999/")

    provider = OllamaProvider()

    assert provider.model == "local-test"
    assert provider.url == "http://localhost:9999"


def test_analysis_result_can_include_or_omit_explanation() -> None:
    result = _result()
    assert result.llm_explanation is None

    class Provider:
        def explain(self, prompt: str) -> str | None:
            return "The full disk is supported by deterministic evidence."

    explained = explain_analysis("no space left", result, Provider())
    assert explained.llm_explanation == "The full disk is supported by deterministic evidence."
    assert explained.category == "hardware"


def test_llm_classification_validates_category_and_confidence() -> None:
    classification = LLMClassification(
        category="hardware", confidence=0.92, root_cause="Disk is full.", evidence=["no space left"]
    )

    assert classification.category == "hardware"
    assert classification.confidence == 0.92


def test_llm_classification_rejects_invalid_category() -> None:
    with pytest.raises(ValidationError):
        LLMClassification(category="database", confidence=0.5, root_cause="Unknown", evidence=[])


def test_llm_classification_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        LLMClassification(category="hardware", confidence=1.1, root_cause="Disk is full.", evidence=[])


def test_ollama_provider_returns_structured_classification() -> None:
    response = MagicMock()
    response.read.return_value = json.dumps(
        {
            "response": json.dumps(
                {
                    "category": "hardware",
                    "confidence": 0.92,
                    "root_cause": "Disk is full.",
                    "evidence": ["no space left on device"],
                }
            )
        }
    ).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response) as mocked_urlopen:
        classification = OllamaProvider(model="test-model", url="http://ollama.test").classify("prompt")

    assert classification is not None
    assert classification.category == "hardware"
    request = mocked_urlopen.call_args.args[0]
    assert json.loads(request.data)["format"] == "json"


def test_ollama_provider_returns_none_for_malformed_classification_json() -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({"response": "not json"}).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response):
        assert OllamaProvider().classify("prompt") is None


def test_ollama_provider_classification_fails_gracefully_when_unavailable() -> None:
    with patch("circa.llm.urlopen", side_effect=OSError("Ollama unavailable")):
        assert OllamaProvider().classify("prompt") is None


def test_classify_failure_returns_provider_classification() -> None:
    class Provider:
        def classify(self, prompt: str) -> LLMClassification:
            return LLMClassification(category="hardware", confidence=0.9, root_cause="Disk full", evidence=["disk"])

    classification = classify_failure(CIFailure(failure_text="no space left"), _result(), Provider())

    assert classification is not None
    assert classification.root_cause == "Disk full"


@pytest.mark.parametrize("body", [[], "text", 3])
def test_ollama_provider_ignores_non_object_responses(body) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response):
        assert OllamaProvider().explain("prompt") is None
        assert OllamaProvider().classify("prompt") is None


def test_ollama_provider_never_opens_non_http_urls() -> None:
    provider = OllamaProvider(url="file:///etc/passwd")
    with patch("circa.llm.urlopen") as mocked_urlopen:
        assert provider.explain("prompt") is None
        assert provider.classify("prompt") is None

    mocked_urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("response_text", "valid"),
    [
        ('{"category": "network", "confidence": 0.4, "root_cause": "DNS", "suggested_fix": "Fix DNS"}', True),
        ('{"category": "cosmic-rays", "confidence": 0.4, "root_cause": "x"}', False),
        ("not json", False),
    ],
    ids=["valid", "invalid-category", "malformed"],
)
def test_ollama_provider_diagnose_validates_structured_output(response_text: str, valid: bool) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({"response": response_text}).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response):
        diagnosis = OllamaProvider().diagnose("prompt")

    assert (diagnosis is not None) is valid
    if diagnosis is not None:
        assert diagnosis.suggested_fix == "Fix DNS"


# The confirmed qwen2.5:1.5b response: valid JSON, but missing_evidence is the string "None".
QWEN_DIAGNOSIS = {
    "category": "software",
    "confidence": 0.0,
    "root_cause": "Insufficient deterministic evidence to identify a root cause.",
    "evidence": [
        "2026-10-24T09:30:07Z [ERROR] signtool rejected bundle: certificate chain expired on 2026-10-23",
        "2026-10-24T09:30:07Z [ERROR] firmware packaging aborted: unsigned bundle is not allowed",
    ],
    "suggested_fix": "Review the signing certificate and firmware packaging configuration.",
    "validation_steps": ["Verify the signing certificate is valid and not expired."],
    "missing_evidence": "None",
}


def _diagnose(response_text: str):
    response = MagicMock()
    response.read.return_value = json.dumps({"response": response_text}).encode()
    response.__enter__.return_value = response
    with patch("circa.llm.urlopen", return_value=response):
        return OllamaProvider(model="qwen2.5:1.5b", url="http://ollama.test").diagnose("prompt")


def test_diagnose_accepts_the_confirmed_missing_evidence_none_string() -> None:
    diagnosis = _diagnose(json.dumps(QWEN_DIAGNOSIS))

    assert diagnosis is not None
    assert diagnosis.missing_evidence == []
    assert diagnosis.evidence == QWEN_DIAGNOSIS["evidence"]


@pytest.mark.parametrize("value", ["None", "none", "N/A", "", "  ", None], ids=repr)
@pytest.mark.parametrize("field", ["evidence", "validation_steps", "missing_evidence"])
def test_diagnose_treats_empty_markers_and_null_as_empty_lists(field: str, value: str | None) -> None:
    diagnosis = _diagnose(json.dumps(QWEN_DIAGNOSIS | {field: value}))

    assert diagnosis is not None
    assert getattr(diagnosis, field) == []


@pytest.mark.parametrize("field", ["evidence", "validation_steps", "missing_evidence"])
def test_diagnose_wraps_a_single_non_empty_string_in_a_list(field: str) -> None:
    diagnosis = _diagnose(json.dumps(QWEN_DIAGNOSIS | {field: "  Signing service expiry logs.  "}))

    assert diagnosis is not None
    assert getattr(diagnosis, field) == ["Signing service expiry logs."]


def test_diagnose_keeps_correctly_typed_lists_unchanged() -> None:
    expected = QWEN_DIAGNOSIS | {"missing_evidence": ["Signing service logs."]}

    diagnosis = _diagnose(json.dumps(expected))

    assert diagnosis is not None
    assert diagnosis.model_dump() == expected


@pytest.mark.parametrize(
    "response_text",
    [
        "not json",
        '```json\n{"category": "network"}\n```',
        json.dumps(["not", "an", "object"]),
        json.dumps(QWEN_DIAGNOSIS | {"missing_evidence": {"detail": "logs"}}),
        json.dumps(QWEN_DIAGNOSIS | {"validation_steps": 3}),
        json.dumps(QWEN_DIAGNOSIS | {"evidence": [1, 2]}),
        json.dumps(QWEN_DIAGNOSIS | {"root_cause": None}),
        json.dumps(QWEN_DIAGNOSIS | {"confidence": "high"}),
    ],
    ids=[
        "invalid-json",
        "fenced",
        "array",
        "object-list",
        "number-list",
        "number-items",
        "null-root",
        "bad-confidence",
    ],
)
def test_diagnose_still_rejects_malformed_responses(response_text: str) -> None:
    assert _diagnose(response_text) is None
