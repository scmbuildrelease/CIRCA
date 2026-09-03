"""Optional local Ollama explanation support."""

from __future__ import annotations

import json
import os
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import AnalysisResult
from .prompts import build_explanation_prompt


class LLMProvider(Protocol):
    """A small interface for optional explanation providers."""

    def explain(self, prompt: str) -> str | None:
        """Return an explanation, or ``None`` when one is unavailable."""


class OllamaProvider:
    """Call a locally running Ollama server through its HTTP API."""

    def __init__(self, model: str | None = None, url: str | None = None, timeout: float = 5.0) -> None:
        self.model = model or os.getenv("CIRCA_LLM_MODEL", "llama3.2")
        self.url = (url or os.getenv("CIRCA_OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout

    def explain(self, prompt: str) -> str | None:
        """Return Ollama's concise response, gracefully handling local failures."""

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
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 -- configured local URL
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return None

        explanation = body.get("response")
        return explanation.strip() if isinstance(explanation, str) and explanation.strip() else None


def explain_analysis(
    failure_text: str,
    result: AnalysisResult,
    provider: LLMProvider | None = None,
) -> AnalysisResult:
    """Attach an optional explanation while preserving deterministic results."""

    explanation = (provider or OllamaProvider()).explain(build_explanation_prompt(failure_text, result))
    return result.model_copy(update={"llm_explanation": explanation})
