import json
from unittest.mock import MagicMock, patch

from circa.llm import OllamaProvider, explain_analysis
from circa.models import AnalysisResult


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
