"""Optional local Ollama explanation support."""

from __future__ import annotations

import json
import os
from http.client import HTTPException
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .models import AnalysisResult, CIFailure, HistoricalIncident, LLMClassification, LLMDiagnosis
from .prompts import build_classification_prompt, build_diagnosis_prompt, build_explanation_prompt


class LLMProvider(Protocol):
    """A small interface for optional explanation providers."""

    def explain(self, prompt: str) -> str | None:
        """Return an explanation, or ``None`` when one is unavailable."""


class DiagnosisProvider(Protocol):
    """A local provider of structured diagnosis hypotheses."""

    model: str

    def diagnose(self, prompt: str) -> LLMDiagnosis | None:
        """Return a validated diagnosis, or ``None`` when unavailable or invalid."""


class OllamaProvider:
    """Call a locally running Ollama server through its HTTP API."""

    def __init__(self, model: str | None = None, url: str | None = None, timeout: float = 5.0) -> None:
        self.model: str = model or os.getenv("CIRCA_LLM_MODEL") or "llama3.2"
        self.url = (url or os.getenv("CIRCA_OLLAMA_URL") or "http://localhost:11434").rstrip("/")
        # Only HTTP(S) URLs are requested, so a misconfigured file:// or custom
        # scheme is treated as an unavailable backend rather than opened.
        self.available = urlsplit(self.url).scheme in {"http", "https"}
        self.timeout = timeout

    def explain(self, prompt: str) -> str | None:
        """Return Ollama's concise response, gracefully handling local failures."""

        if not self.available:
            return None

        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
        ).encode("utf-8")
        request = Request(
            f"{self.url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # Bandit B310: __init__ restricts self.url to http(s).
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, HTTPException, URLError, ValueError):
            return None

        explanation = body.get("response") if isinstance(body, dict) else None
        return explanation.strip() if isinstance(explanation, str) and explanation.strip() else None

    def _generate_json(self, prompt: str) -> str | None:
        """Return Ollama's JSON-mode response text, or ``None`` when unavailable."""

        if not self.available:
            return None

        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        request = Request(
            f"{self.url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # Bandit B310: __init__ restricts self.url to http(s).
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, HTTPException, URLError, ValueError):
            return None
        response_text = body.get("response") if isinstance(body, dict) else None
        return response_text if isinstance(response_text, str) else None

    def classify(self, prompt: str) -> LLMClassification | None:
        """Return a validated structured local classification, when available."""

        response_text = self._generate_json(prompt)
        try:
            return LLMClassification.model_validate_json(response_text) if response_text is not None else None
        except ValidationError:
            return None

    def diagnose(self, prompt: str) -> LLMDiagnosis | None:
        """Return a validated structured diagnosis hypothesis, when available."""

        response_text = self._generate_json(prompt)
        try:
            return LLMDiagnosis.model_validate_json(response_text) if response_text is not None else None
        except ValidationError:
            return None


def explain_analysis(
    failure_text: str,
    result: AnalysisResult,
    provider: LLMProvider | None = None,
) -> AnalysisResult:
    """Attach an optional explanation while preserving deterministic results."""

    explanation = (provider or OllamaProvider()).explain(build_explanation_prompt(failure_text, result))
    return result.model_copy(update={"llm_explanation": explanation})


def classify_failure(
    failure: CIFailure,
    result: AnalysisResult,
    provider: OllamaProvider | None = None,
) -> LLMClassification | None:
    """Return an optional independent LLM opinion without changing deterministic analysis."""

    return (provider or OllamaProvider()).classify(build_classification_prompt(failure, result))


def diagnose_failure(
    failure: CIFailure,
    result: AnalysisResult,
    references: list[HistoricalIncident],
    provider: DiagnosisProvider,
) -> LLMDiagnosis | None:
    """Request one bounded diagnosis hypothesis; deterministic analysis stays authoritative."""

    return provider.diagnose(build_diagnosis_prompt(failure, result, references))
