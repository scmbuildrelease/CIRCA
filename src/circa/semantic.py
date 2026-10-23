"""Optional local semantic retrieval for historical CI incidents."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from http.client import HTTPException
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import CIFailure, HistoricalIncident, SemanticIncidentMatch


class EmbeddingProvider(Protocol):
    """A replaceable local embedding backend."""

    def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """Return one embedding per text, or ``None`` when unavailable."""


class SemanticRetriever(Protocol):
    """Anything that ranks incidents semantically, like ``SemanticIncidentRetriever``."""

    def retrieve(
        self,
        failure: CIFailure,
        incidents: Iterable[HistoricalIncident],
        limit: int = 3,
    ) -> list[SemanticIncidentMatch] | None:
        """Return ranked matches, or ``None`` when the backend is unavailable."""


class OllamaEmbeddingProvider:
    """Generate embeddings through Ollama's local embedding API."""

    def __init__(self, model: str | None = None, url: str | None = None, timeout: float = 5.0) -> None:
        self.model = model or os.getenv("CIRCA_EMBEDDING_MODEL", "nomic-embed-text")
        self.url = (url or os.getenv("CIRCA_OLLAMA_URL") or "http://localhost:11434").rstrip("/")
        # Only HTTP(S) URLs are requested, so a misconfigured file:// or custom
        # scheme is treated as an unavailable backend rather than opened.
        self.available = urlsplit(self.url).scheme in {"http", "https"}
        self.timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """Return local embeddings, gracefully handling unavailable Ollama models."""

        if not texts:
            return []
        if not self.available:
            return None
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = Request(
            f"{self.url}/api/embed",
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

        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            return None
        try:
            return [[float(value) for value in embedding] for embedding in embeddings]
        except (TypeError, ValueError):
            return None


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, treating empty or zero vectors as unrelated."""

    if len(left) != len(right) or not left:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _incident_text(incident: HistoricalIncident) -> str:
    environment = " ".join(f"{key} {value}" for key, value in incident.environment.items())
    return " ".join([incident.title, incident.summary, *incident.keywords, environment])


class SemanticIncidentRetriever:
    """Rank local incidents in memory using a replaceable embedding backend."""

    def __init__(self, provider: EmbeddingProvider, threshold: float = 0.5) -> None:
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between -1.0 and 1.0")
        self.provider = provider
        self.threshold = threshold

    def retrieve(
        self,
        failure: CIFailure,
        incidents: Iterable[HistoricalIncident],
        limit: int = 3,
    ) -> list[SemanticIncidentMatch] | None:
        """Return thresholded, ranked matches or ``None`` if embeddings are unavailable."""

        if limit < 1:
            return []
        available_incidents = list(incidents)
        if not available_incidents:
            return []

        embeddings = self.provider.embed(
            [failure.failure_text, *(_incident_text(item) for item in available_incidents)]
        )
        if embeddings is None or len(embeddings) != len(available_incidents) + 1:
            return None
        failure_embedding, incident_embeddings = embeddings[0], embeddings[1:]
        matches = [
            SemanticIncidentMatch(incident=incident, similarity=cosine_similarity(failure_embedding, embedding))
            for incident, embedding in zip(available_incidents, incident_embeddings, strict=True)
        ]
        matches = [match for match in matches if match.similarity >= self.threshold]
        matches.sort(key=lambda match: (-match.similarity, match.incident.id))
        return matches[:limit]
