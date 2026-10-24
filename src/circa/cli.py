"""Command-line interface for local CIRCA analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from .analyzer import analyze_failure
from .benchmark import BenchmarkMethodResult, load_evaluation_cases, run_retrieval_benchmark
from .hybrid import apply_hybrid_scoring
from .knowledge import load_incidents
from .knowledge_store import (
    KnowledgeError,
    KnowledgeFile,
    add_incident,
    load_analysis_incidents,
    load_knowledge,
    save_knowledge,
    serialize_incidents,
    supersede_incident,
    update_incident,
    verify_incident,
)
from .llm import classify_failure, explain_analysis
from .models import AnalysisResult, CIFailure, HistoricalIncident, LLMClassification, SemanticIncidentMatch
from .pipeline import analyze_log
from .resources import data_path
from .semantic import OllamaEmbeddingProvider, SemanticIncidentRetriever

app = typer.Typer(help="Analyze CI failures using local deterministic evidence.")
knowledge_app = typer.Typer(help="Manage a human-reviewed team incident knowledge file.")
app.add_typer(knowledge_app, name="knowledge")

_HYBRID_FIELDS = {"llm_classification", "final_category", "final_confidence", "final_root_cause"}


@app.callback()
def main() -> None:
    """CIRCA command group."""


def _print_report(result: AnalysisResult, heading: str = "CIRCA Analysis Report") -> None:
    """Print one concise, human-readable analysis report."""

    typer.echo(heading)
    typer.echo(f"Category: {result.category}")
    typer.echo(f"Confidence: {result.confidence:.2f}")
    typer.echo(f"Root cause: {result.root_cause}")
    typer.echo("Evidence:")
    if result.evidence:
        for item in result.evidence:
            typer.echo(f"- {item}")
    else:
        typer.echo("- No strong deterministic evidence.")
    typer.echo("Historical matches:")
    if result.matched_incident_ids:
        for incident_id in result.matched_incident_ids:
            typer.echo(f"- {incident_id}")
    else:
        typer.echo("- None")
    typer.echo(f"Recommendation: {result.recommendation}")
    typer.echo(f"Owner: {result.owner}")
    typer.echo(f"Fingerprint: {result.fingerprint}")
    if result.llm_explanation:
        typer.echo(f"LLM explanation: {result.llm_explanation}")
    if result.performance_assessment:
        assessment = result.performance_assessment
        typer.echo("Performance Assessment")
        typer.echo(f"Failure type: {assessment.failure_type}")
        typer.echo(f"Candidate cause (hypothesis): {assessment.candidate_cause}")
        typer.echo(f"Recommended action: {assessment.recommended_action or 'none'}")
        for measurement in assessment.measurements:
            typer.echo(
                f"Measurement: {measurement.metric}={measurement.actual_value:g} {measurement.unit or ''} "
                f"(expected {measurement.expected_range})"
            )
        if assessment.missing_evidence:
            typer.echo("Missing performance evidence:")
            for item in assessment.missing_evidence:
                typer.echo(f"- {item}")
        typer.echo(f"Performance recommendation: {assessment.recommendation}")


def _print_llm_classification(classification: LLMClassification) -> None:
    """Print the independent LLM classification separately from deterministic output."""

    typer.echo("LLM Classification")
    typer.echo(f"Category: {classification.category}")
    typer.echo(f"Confidence: {classification.confidence:.2f}")
    typer.echo(f"Root Cause: {classification.root_cause}")
    typer.echo("Evidence:")
    if classification.evidence:
        for item in classification.evidence:
            typer.echo(f"- {item}")
    else:
        typer.echo("- None")


def _print_hybrid_analysis(result: AnalysisResult) -> None:
    """Print the final deterministic-first hybrid result."""

    typer.echo("Final Hybrid Analysis")
    typer.echo(f"Category: {result.final_category}")
    typer.echo(f"Confidence: {result.final_confidence:.2f}")
    typer.echo(f"Root Cause: {result.final_root_cause}")


def _print_semantic_matches(matches: list[SemanticIncidentMatch]) -> None:
    """Print ranked local semantic incident matches."""

    typer.echo("Semantic Historical Matches")
    if not matches:
        typer.echo("- None above the similarity threshold.")
        return
    for match in matches:
        typer.echo(f"- {match.incident.id}: {match.similarity:.2f}")


def _format_metric(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.3f}"


def _print_benchmark_method(method: BenchmarkMethodResult, top_k: int) -> None:
    """Print retrieval-only metrics and their documented denominators."""

    metrics = method.metrics
    typer.echo(f"{method.name.title()} retrieval ({method.status})")
    typer.echo(f"  evaluated: {metrics.evaluated_cases}; skipped: {metrics.skipped_cases}")
    typer.echo(
        f"  relevant-label cases: {metrics.relevant_cases}; no-known-relevance: {metrics.no_known_relevance_cases}"
    )
    typer.echo(f"  Top-1 accuracy (relevant-label cases): {_format_metric(metrics.top_1_accuracy)}")
    typer.echo(f"  Recall@{top_k} (relevant-label cases): {_format_metric(metrics.recall_at_k)}")
    typer.echo(f"  MRR (relevant-label cases): {_format_metric(metrics.mean_reciprocal_rank)}")
    typer.echo(f"  no-result rate (evaluated cases): {_format_metric(metrics.no_result_rate)}")
    typer.echo(f"  coverage (all dataset cases): {_format_metric(metrics.evaluation_coverage)}")


KnowledgeOption = Annotated[
    Path | None,
    typer.Option(
        "--knowledge",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Team knowledge file; its verified incidents are matched alongside the packaged ones. Never written.",
    ),
]


def _analysis_incidents(knowledge: Path | None) -> list[HistoricalIncident] | None:
    """Return ``None`` (packaged defaults) without --knowledge, keeping behavior unchanged."""

    if knowledge is None:
        return None
    try:
        return load_analysis_incidents(knowledge)
    except KnowledgeError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@app.command()
def analyze(
    failure_json: Annotated[Path, typer.Argument(exists=True, readable=True, help="Path to a CIFailure JSON file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print the analysis result as JSON.")] = False,
    llm: Annotated[bool, typer.Option("--llm", help="Add an optional local Ollama explanation.")] = False,
    llm_classify: Annotated[
        bool, typer.Option("--llm-classify", help="Add an optional independent local LLM classification.")
    ] = False,
    semantic_retrieve: Annotated[
        bool, typer.Option("--semantic-retrieve", help="Retrieve similar incidents with optional local embeddings.")
    ] = False,
    knowledge: KnowledgeOption = None,
) -> None:
    """Analyze a local CIFailure JSON file."""

    failure = CIFailure.model_validate_json(failure_json.read_text(encoding="utf-8"))
    analysis_incidents = _analysis_incidents(knowledge)
    deterministic_result = analyze_failure(failure, analysis_incidents)
    if llm:
        deterministic_result = explain_analysis(failure.failure_text, deterministic_result)
    classification = classify_failure(failure, deterministic_result) if llm_classify else None
    result = apply_hybrid_scoring(deterministic_result, classification) if llm_classify else deterministic_result
    semantic_matches = None
    if semantic_retrieve:
        incidents = analysis_incidents or load_incidents(data_path("incidents.json"))
        semantic_matches = SemanticIncidentRetriever(OllamaEmbeddingProvider()).retrieve(failure, incidents)
    if llm_classify and classification is None:
        typer.echo("Warning: LLM classification unavailable.", err=True)
    if semantic_retrieve and semantic_matches is None:
        typer.echo("Warning: semantic retrieval unavailable.", err=True)
    if json_output:
        if llm_classify:
            payload: dict[str, Any] = {
                "deterministic_analysis": deterministic_result.model_dump(exclude=_HYBRID_FIELDS),
                "llm_classification": classification.model_dump() if classification else None,
                "final_hybrid_analysis": {
                    "category": result.final_category,
                    "confidence": result.final_confidence,
                    "root_cause": result.final_root_cause,
                },
            }
            if semantic_retrieve:
                payload["semantic_matches"] = (
                    [match.model_dump() for match in semantic_matches] if semantic_matches is not None else None
                )
            typer.echo(json.dumps(payload, indent=2))
        else:
            payload = dict(result.model_dump(exclude=_HYBRID_FIELDS))
            if semantic_retrieve:
                payload["semantic_matches"] = (
                    [match.model_dump() for match in semantic_matches] if semantic_matches is not None else None
                )
            typer.echo(json.dumps(payload, indent=2))
        return
    _print_report(result, "Deterministic Analysis" if llm_classify else "CIRCA Analysis Report")
    if classification:
        typer.echo()
        _print_llm_classification(classification)
        typer.echo()
        _print_hybrid_analysis(result)
    if semantic_retrieve and semantic_matches is not None:
        typer.echo()
        _print_semantic_matches(semantic_matches)


@app.command("analyze-log")
def analyze_log_file(
    raw_log_file: Annotated[Path, typer.Argument(exists=True, readable=True, help="Path to a raw CI log file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print the analysis result as JSON.")] = False,
    knowledge: KnowledgeOption = None,
) -> None:
    """Extract failure evidence from and analyze a local raw CI log."""

    result = analyze_log(raw_log_file.read_text(encoding="utf-8"), incidents=_analysis_incidents(knowledge))
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    _print_report(result)


@app.command()
def benchmark(
    top_k: Annotated[int, typer.Option("--top-k", min=1, help="Number of incidents to retrieve per case.")] = 3,
    dataset: Annotated[
        Path | None, typer.Option("--dataset", help="Path to a synthetic evaluation dataset JSON file.")
    ] = None,
) -> None:
    """Benchmark local historical retrieval against labeled synthetic cases."""

    incidents = load_incidents(data_path("incidents.json"))
    cases = load_evaluation_cases(dataset or data_path("retrieval_evaluation.json"), incidents)
    report = run_retrieval_benchmark(
        cases, incidents, top_k=top_k, semantic_retriever=SemanticIncidentRetriever(OllamaEmbeddingProvider())
    )
    typer.echo(f"CIRCA Retrieval Benchmark: {report.dataset_size} dataset cases; Top-K: {report.top_k}")
    for method in report.methods:
        typer.echo()
        _print_benchmark_method(method, report.top_k)


KnowledgeFileOption = Annotated[
    Path, typer.Option("--knowledge", dir_okay=False, help="Team knowledge JSON file to read and update.")
]
YesOption = Annotated[bool, typer.Option("--yes", help="Write without the interactive confirmation prompt.")]
AllowSimilarOption = Annotated[
    bool, typer.Option("--allow-similar", help="Proceed even when a near-duplicate incident is reported.")
]


def _fail(error: Exception) -> typer.Exit:
    typer.echo(f"Error: {error}", err=True)
    return typer.Exit(1)


def _load_draft(draft_json: Path) -> dict[str, Any]:
    try:
        draft = json.loads(draft_json.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(KnowledgeError(f"{draft_json} is not valid JSON: {error}")) from error
    if not isinstance(draft, dict):
        raise _fail(KnowledgeError(f"{draft_json} must contain one JSON incident object"))
    return draft


def _confirm_and_save(
    knowledge: KnowledgeFile, incidents: list[HistoricalIncident], incident: HistoricalIncident, action: str, yes: bool
) -> None:
    """Show the exact record, require confirmation, then write atomically."""

    typer.echo(f"{action} incident '{incident.id}' ({incident.status}) in {knowledge.path}:")
    typer.echo(serialize_incidents([incident]).strip())
    if not yes and not typer.confirm("Write this change?", default=False):
        typer.echo("Aborted; nothing was written.", err=True)
        raise typer.Exit(1)
    try:
        save_knowledge(knowledge, incidents)
    except KnowledgeError as error:
        raise _fail(error) from error
    typer.echo(f"Wrote {knowledge.path}")


@knowledge_app.command("add")
def knowledge_add(
    draft_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Human-written incident draft.")],
    knowledge: KnowledgeFileOption,
    yes: YesOption = False,
    allow_similar: AllowSimilarOption = False,
) -> None:
    """Add a new incident; it always starts as pending_review. Creates the file if needed."""

    try:
        current = load_knowledge(knowledge, must_exist=False)
        incidents, incident, warnings = add_incident(current, _load_draft(draft_json), allow_similar=allow_similar)
    except KnowledgeError as error:
        raise _fail(error) from error
    for warning in warnings:
        typer.echo(f"Warning: {warning}", err=True)
    _confirm_and_save(current, incidents, incident, "Add", yes)


@knowledge_app.command("update")
def knowledge_update(
    draft_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Revised incident draft.")],
    knowledge: KnowledgeFileOption,
    yes: YesOption = False,
    allow_similar: AllowSimilarOption = False,
) -> None:
    """Replace a pending_review incident with a revised draft of the same ID."""

    try:
        current = load_knowledge(knowledge)
        incidents, incident, warnings = update_incident(current, _load_draft(draft_json), allow_similar=allow_similar)
    except KnowledgeError as error:
        raise _fail(error) from error
    for warning in warnings:
        typer.echo(f"Warning: {warning}", err=True)
    _confirm_and_save(current, incidents, incident, "Update", yes)


@knowledge_app.command("verify")
def knowledge_verify(
    incident_id: Annotated[str, typer.Argument(help="ID of a pending_review incident.")],
    knowledge: KnowledgeFileOption,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Name of the human reviewer approving it.")],
    yes: YesOption = False,
) -> None:
    """Mark a pending_review incident verified after human review."""

    try:
        current = load_knowledge(knowledge)
        incidents, incident = verify_incident(current, incident_id, reviewer=reviewer)
    except KnowledgeError as error:
        raise _fail(error) from error
    _confirm_and_save(current, incidents, incident, "Verify", yes)


@knowledge_app.command("supersede")
def knowledge_supersede(
    incident_id: Annotated[str, typer.Argument(help="ID of the verified incident being replaced.")],
    by: Annotated[str, typer.Option("--by", help="ID of the verified incident that replaces it.")],
    knowledge: KnowledgeFileOption,
    yes: YesOption = False,
) -> None:
    """Retire a verified incident in favor of a newer verified one."""

    try:
        current = load_knowledge(knowledge)
        incidents, incident = supersede_incident(current, incident_id, by=by)
    except KnowledgeError as error:
        raise _fail(error) from error
    _confirm_and_save(current, incidents, incident, "Supersede", yes)


@knowledge_app.command("list")
def knowledge_list(knowledge: KnowledgeFileOption) -> None:
    """List team incidents with their status and provenance."""

    try:
        current = load_knowledge(knowledge)
    except KnowledgeError as error:
        raise _fail(error) from error
    for incident in sorted(current.incidents, key=lambda item: item.id):
        provenance = "synthetic" if incident.synthetic else "real"
        typer.echo(f"{incident.id}\t{incident.status}\t{provenance}\t{incident.title}")


@knowledge_app.command("validate")
def knowledge_validate(
    knowledge_json: Annotated[Path, typer.Argument(dir_okay=False, help="Team knowledge JSON file to check.")],
) -> None:
    """Validate schema, lifecycle rules, IDs, and duplicate fingerprints; exit 1 on errors."""

    try:
        current = load_knowledge(knowledge_json)
    except KnowledgeError as error:
        raise _fail(error) from error
    counts = {
        status: sum(item.status == status for item in current.incidents)
        for status in ("pending_review", "verified", "superseded")
    }
    typer.echo(f"{knowledge_json}: valid; " + ", ".join(f"{count} {status}" for status, count in counts.items()))
