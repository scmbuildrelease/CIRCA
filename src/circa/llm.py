"""Optional local Ollama explanation support."""

from __future__ import annotations

import json
import os
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .models import AnalysisResult, CIFailure, HistoricalIncident, LLMClassification, LLMDiagnosis
from .prompts import build_classification_prompt, build_diagnosis_prompt, build_explanation_prompt

# List-valued diagnosis fields, and the strings small models use to mean "nothing".
_DIAGNOSIS_LIST_FIELDS = ("evidence", "validation_steps", "missing_evidence")
_EMPTY_LIST_MARKERS = frozenset({"", "none", "n/a", "null"})


def _normalize_diagnosis_lists(data: object) -> object:
    """Fix common small-model list mistakes before strict validation.

    ``null`` or an "empty" marker string becomes ``[]``; another string becomes a
    one-item list. Any other type is left unchanged so validation rejects it.
    """

    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    for field in _DIAGNOSIS_LIST_FIELDS:
        value = normalized.get(field)
        if field in normalized and value is None:
            normalized[field] = []
        elif isinstance(value, str):
            text = value.strip()
            normalized[field] = [] if text.lower() in _EMPTY_LIST_MARKERS else [text]
    return normalized


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
        # Why the last structured request returned nothing, and whether it reached the model at all.
        self.last_error: str | None = None
        self.model_ran = False

    def _server(self) -> str:
        """The server's scheme, host, and port: never a user name or password from the URL."""

        parts = urlsplit(self.url)
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        return f"{parts.scheme}://{parts.hostname or ''}{port}"

    def installed_models(self) -> list[str] | None:
        """Return the model names the server has installed (``GET /api/tags``), or ``None`` if unreachable.

        Listing models runs no model, so it is safe for setup checks.
        """

        if not self.available:
            return None
        try:
            # Bandit B310: __init__ restricts self.url to http(s).
            with urlopen(Request(f"{self.url}/api/tags", method="GET"), timeout=self.timeout) as response:  # nosec B310
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, HTTPException, URLError, ValueError):
            return None
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return None
        return [str(model["name"]) for model in models if isinstance(model, dict) and model.get("name")]

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
        """Return Ollama's JSON-mode response text, or ``None`` when unavailable (``last_error`` says why)."""

        self.last_error, self.model_ran = None, False
        if not self.available:
            self.last_error = "CIRCA_OLLAMA_URL is not an http:// or https:// URL"
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
        except HTTPError as error:
            # Ollama answers 404 for a model that is not installed; it never ran.
            self.model_ran = error.code != 404
            self.last_error = (
                f"model '{self.model}' is not installed on the Ollama server; run 'ollama pull {self.model}' "
                "or set CIRCA_LLM_MODEL to an installed model ('ollama list')"
                if error.code == 404
                else f"the Ollama server returned HTTP {error.code}"
            )
            return None
        except (OSError, HTTPException, URLError, ValueError) as error:
            reason = error.reason if isinstance(error, URLError) else error
            if isinstance(reason, TimeoutError):
                self.model_ran = True
                self.last_error = f"model '{self.model}' did not reply within {self.timeout:g} seconds"
            elif isinstance(error, (OSError, URLError)):
                self.last_error = (
                    f"no Ollama server is reachable at {self._server()}; start it ('ollama serve') "
                    "or set CIRCA_OLLAMA_URL"
                )
            else:
                self.model_ran = True
                self.last_error = (
                    "the Ollama server's reply was not valid JSON"
                    if isinstance(error, ValueError)
                    else "the Ollama server's reply could not be read"
                )
            return None
        self.model_ran = True
        response_text = body.get("response") if isinstance(body, dict) else None
        if not isinstance(response_text, str):
            self.last_error = "the Ollama server's reply had no response text"
            return None
        return response_text

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
        if response_text is None:
            return None
        try:
            return LLMDiagnosis.model_validate(_normalize_diagnosis_lists(json.loads(response_text)))
        except (ValueError, ValidationError):
            self.last_error = f"model '{self.model}' did not return a valid structured diagnosis"
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
