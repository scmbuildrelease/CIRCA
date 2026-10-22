"""Reproducible evaluation of local historical-incident retrieval."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .knowledge import find_similar_incidents, load_incidents
from .models import CauseCategory, CIFailure, HistoricalIncident
from .retrieval import retrieve_historical_candidates
from .resources import data_path
from .semantic import SemanticIncidentRetriever


class EvaluationCase(BaseModel):
    """One synthetic retrieval query with zero or more supported relevance labels."""

    id: str = Field(min_length=1)
    failure_text: str = Field(min_length=1)
    expected_category: CauseCategory
    relevant_incident_ids: list[str] = Field(default_factory=list)
    relevance_note: str | None = None
    synthetic: Literal[True] = True


class RetrievalMetrics(BaseModel):
    """Metrics with explicit denominators for incident-retrieval evaluation."""

    evaluated_cases: int = Field(ge=0)
    skipped_cases: int = Field(ge=0)
    relevant_cases: int = Field(ge=0)
    no_known_relevance_cases: int = Field(ge=0)
    top_1_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    recall_at_k: float | None = Field(default=None, ge=0.0, le=1.0)
    mean_reciprocal_rank: float | None = Field(default=None, ge=0.0, le=1.0)
    no_result_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    evaluation_coverage: float | None = Field(default=None, ge=0.0, le=1.0)


class BenchmarkMethodResult(BaseModel):
    """Metrics and availability state for one retrieval method."""

    name: str
    status: str
    metrics: RetrievalMetrics


class BenchmarkReport(BaseModel):
    """All retrieval benchmark results for one fixed dataset and top-K value."""

    dataset_size: int = Field(ge=0)
    top_k: int = Field(ge=1)
    methods: list[BenchmarkMethodResult]


def load_evaluation_cases(
    path: str | Path,
    incidents: Iterable[HistoricalIncident] | None = None,
) -> list[EvaluationCase]:
    """Load and validate synthetic cases against the available incident IDs."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid evaluation dataset JSON in {source}: {error.msg}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError(f"Invalid evaluation dataset {source}: expected an object with a 'cases' list.")

    available_incidents = incidents if incidents is not None else load_incidents(data_path("incidents.json"))
    known_ids = {incident.id for incident in available_incidents}
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(payload["cases"]):
        try:
            case = EvaluationCase.model_validate(record)
        except ValidationError as error:
            raise ValueError(f"Invalid evaluation case at index {index} in {source}: {error}") from error
        if case.id in seen_ids:
            raise ValueError(f"Duplicate evaluation case ID '{case.id}' in {source}.")
        unknown_ids = sorted(set(case.relevant_incident_ids) - known_ids)
        if unknown_ids:
            raise ValueError(
                f"Evaluation case '{case.id}' references unknown historical incident ID(s): {', '.join(unknown_ids)}."
            )
        seen_ids.add(case.id)
        cases.append(case)
    return cases


def calculate_retrieval_metrics(
    cases: Sequence[EvaluationCase],
    retrieved_ids: Sequence[Sequence[str] | None],
    top_k: int,
) -> RetrievalMetrics:
    """Calculate retrieval metrics without treating unlabeled cases as misses.

    Top-1, Recall@K, and MRR use only cases with one or more known relevant
    incident IDs. No-result rate uses all successfully evaluated cases.
    Coverage uses every dataset case, including deliberately ambiguous cases.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if len(cases) != len(retrieved_ids):
        raise ValueError("cases and retrieved_ids must have the same length")

    evaluated = [(case, ids[:top_k]) for case, ids in zip(cases, retrieved_ids, strict=True) if ids is not None]
    eligible = [(case, ids) for case, ids in evaluated if case.relevant_incident_ids]
    no_results = sum(not ids for _, ids in evaluated)
    top_1_hits = sum(bool(ids) and ids[0] in case.relevant_incident_ids for case, ids in eligible)
    recall_hits = sum(bool(set(ids) & set(case.relevant_incident_ids)) for case, ids in eligible)
    reciprocal_ranks = [
        next((1 / rank for rank, incident_id in enumerate(ids, start=1) if incident_id in case.relevant_incident_ids), 0.0)
        for case, ids in eligible
    ]
    eligible_count = len(eligible)
    evaluated_count = len(evaluated)
    dataset_size = len(cases)
    return RetrievalMetrics(
        evaluated_cases=evaluated_count,
        skipped_cases=dataset_size - evaluated_count,
        relevant_cases=eligible_count,
        no_known_relevance_cases=evaluated_count - eligible_count,
        top_1_accuracy=top_1_hits / eligible_count if eligible_count else None,
        recall_at_k=recall_hits / eligible_count if eligible_count else None,
        mean_reciprocal_rank=sum(reciprocal_ranks) / eligible_count if eligible_count else None,
        no_result_rate=no_results / evaluated_count if evaluated_count else None,
        evaluation_coverage=evaluated_count / dataset_size if dataset_size else None,
    )


def _failure(case: EvaluationCase) -> CIFailure:
    return CIFailure(failure_text=case.failure_text)


def run_retrieval_benchmark(
    cases: Sequence[EvaluationCase],
    incidents: Iterable[HistoricalIncident],
    *,
    top_k: int = 3,
    semantic_retriever: SemanticIncidentRetriever | None = None,
) -> BenchmarkReport:
    """Run existing keyword, semantic, and combined retrieval on labeled cases.

    A semantic ``None`` result means its embedding backend was unavailable for
    that case. Combined retrieval still returns its documented keyword fallback
    and labels that condition in its method status.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    available_incidents = list(incidents)
    keyword_ids: list[list[str]] = []
    semantic_ids: list[list[str] | None] = []
    combined_ids: list[list[str]] = []
    semantic_unavailable = 0
    active_semantic_retriever = semantic_retriever

    for case in cases:
        failure = _failure(case)
        keyword_ids.append([incident.id for incident in find_similar_incidents(failure, available_incidents, top_k)])
        semantic_matches = (
            active_semantic_retriever.retrieve(failure, available_incidents, top_k)
            if active_semantic_retriever
            else None
        )
        if semantic_matches is None:
            semantic_ids.append(None)
            semantic_unavailable += 1
            # Avoid retrying an unavailable local backend for every case.
            active_semantic_retriever = None
        else:
            semantic_ids.append([match.incident.id for match in semantic_matches])
        combined = retrieve_historical_candidates(
            failure,
            available_incidents,
            semantic_retriever=active_semantic_retriever,
            keyword_limit=top_k,
            semantic_limit=top_k,
            limit=top_k,
        )
        combined_ids.append([candidate.incident.id for candidate in combined])

    semantic_status = "available" if semantic_unavailable == 0 else "unavailable" if semantic_unavailable == len(cases) else "partial"
    combined_status = "combined" if semantic_unavailable == 0 else f"keyword fallback for {semantic_unavailable} case(s)"
    return BenchmarkReport(
        dataset_size=len(cases),
        top_k=top_k,
        methods=[
            BenchmarkMethodResult(
                name="keyword", status="available", metrics=calculate_retrieval_metrics(cases, keyword_ids, top_k)
            ),
            BenchmarkMethodResult(
                name="semantic", status=semantic_status, metrics=calculate_retrieval_metrics(cases, semantic_ids, top_k)
            ),
            BenchmarkMethodResult(
                name="combined", status=combined_status, metrics=calculate_retrieval_metrics(cases, combined_ids, top_k)
            ),
        ],
    )
