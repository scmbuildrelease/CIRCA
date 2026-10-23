import json
from unittest.mock import MagicMock, patch

import pytest

from circa.analyzer import analyze_failure
from circa.knowledge import load_incidents
from circa.models import CIFailure, HistoricalIncident
from circa.resources import data_path
from circa.semantic import OllamaEmbeddingProvider, SemanticIncidentRetriever, cosine_similarity


class StaticEmbeddingProvider:
    def __init__(self, embeddings: list[list[float]] | None) -> None:
        self.embeddings = embeddings
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.calls.append(texts)
        return self.embeddings


def _incident(identifier: str, category: str = "hardware") -> HistoricalIncident:
    return HistoricalIncident(
        id=identifier,
        title=identifier,
        category=category,
        summary=f"{identifier} summary",
        recommendation="Investigate it.",
        synthetic=True,
    )


def test_cosine_similarity_handles_related_and_zero_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_ollama_embedding_provider_reads_local_embeddings() -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({"embeddings": [[1, 0], [0, 1]]}).encode()
    response.__enter__.return_value = response
    with patch("circa.semantic.urlopen", return_value=response) as mocked_urlopen:
        embeddings = OllamaEmbeddingProvider(model="test-embedding", url="http://ollama.test").embed(["one", "two"])

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    request = mocked_urlopen.call_args.args[0]
    assert request.full_url == "http://ollama.test/api/embed"
    assert json.loads(request.data) == {"model": "test-embedding", "input": ["one", "two"]}


def test_semantic_retrieval_ranks_relevant_incident_first() -> None:
    provider = StaticEmbeddingProvider([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])
    matches = SemanticIncidentRetriever(provider, threshold=0.1).retrieve(
        CIFailure(failure_text="worker disk is full"), [_incident("relevant"), _incident("unrelated")]
    )

    assert matches is not None
    assert [match.incident.id for match in matches] == ["relevant"]
    assert matches[0].similarity > 0.9
    assert len(provider.calls) == 1


def test_semantic_retrieval_applies_similarity_threshold() -> None:
    provider = StaticEmbeddingProvider([[1.0, 0.0], [0.6, 0.8]])
    matches = SemanticIncidentRetriever(provider, threshold=0.7).retrieve(
        CIFailure(failure_text="worker disk is full"), [_incident("weak")]
    )

    assert matches == []


def test_semantic_retrieval_returns_empty_for_no_incidents() -> None:
    provider = StaticEmbeddingProvider([])

    assert SemanticIncidentRetriever(provider).retrieve(CIFailure(failure_text="failure"), []) == []
    assert provider.calls == []


def test_semantic_retrieval_returns_none_when_embedding_backend_is_unavailable() -> None:
    provider = StaticEmbeddingProvider(None)

    assert (
        SemanticIncidentRetriever(provider).retrieve(CIFailure(failure_text="failure"), [_incident("incident")]) is None
    )


def test_ollama_embedding_provider_fails_gracefully_when_unavailable() -> None:
    with patch("circa.semantic.urlopen", side_effect=OSError("Ollama unavailable")):
        assert OllamaEmbeddingProvider().embed(["failure"]) is None


def test_unavailable_semantic_retrieval_does_not_change_keyword_analysis() -> None:
    incidents = load_incidents(data_path("incidents.json"))
    failure = CIFailure(
        failure_text="Dependency installation hit a package registry connection timeout",
        environment={"network": "external"},
    )

    result = analyze_failure(failure, incidents)

    assert result.category == "network"
    assert result.matched_incident_ids == ["synthetic-network-timeout-005"]


def test_ollama_embedding_provider_never_opens_non_http_urls() -> None:
    with patch("circa.semantic.urlopen") as mocked_urlopen:
        assert OllamaEmbeddingProvider(url="file:///etc/passwd").embed(["text"]) is None

    mocked_urlopen.assert_not_called()
