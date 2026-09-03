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
        "model": "test-model", "prompt": "prompt", "stream": False, "options": {"temperature": 0}
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
