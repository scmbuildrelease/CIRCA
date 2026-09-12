"""Command-line interface for local CIRCA analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .analyzer import analyze_failure
from .hybrid import apply_hybrid_scoring
from .llm import classify_failure, explain_analysis
from .knowledge import load_incidents
from .models import AnalysisResult, CIFailure, LLMClassification, SemanticIncidentMatch
from .pipeline import analyze_log
from .semantic import OllamaEmbeddingProvider, SemanticIncidentRetriever

app = typer.Typer(help="Analyze CI failures using local deterministic evidence.")

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
) -> None:
    """Analyze a local CIFailure JSON file."""

    failure = CIFailure.model_validate_json(failure_json.read_text(encoding="utf-8"))
    deterministic_result = analyze_failure(failure)
    if llm:
        deterministic_result = explain_analysis(failure.failure_text, deterministic_result)
    classification = classify_failure(failure, deterministic_result) if llm_classify else None
    result = apply_hybrid_scoring(deterministic_result, classification) if llm_classify else deterministic_result
    semantic_matches = None
    if semantic_retrieve:
        incidents = load_incidents(Path(__file__).parents[2] / "data" / "incidents.json")
        semantic_matches = SemanticIncidentRetriever(OllamaEmbeddingProvider()).retrieve(failure, incidents)
    if llm_classify and classification is None:
        typer.echo("Warning: LLM classification unavailable.", err=True)
    if semantic_retrieve and semantic_matches is None:
        typer.echo("Warning: semantic retrieval unavailable.", err=True)
    if json_output:
        if llm_classify:
            payload = {
                "deterministic_analysis": deterministic_result.model_dump(exclude=_HYBRID_FIELDS),
                "llm_classification": classification.model_dump() if classification else None,
                "final_hybrid_analysis": {
                    "category": result.final_category,
                    "confidence": result.final_confidence,
                    "root_cause": result.final_root_cause,
                },
            }
            if semantic_retrieve:
                payload["semantic_matches"] = [match.model_dump() for match in semantic_matches] if semantic_matches else []
            typer.echo(
                json.dumps(payload, indent=2)
            )
        else:
            payload = result.model_dump(exclude=_HYBRID_FIELDS)
            if semantic_retrieve:
                payload["semantic_matches"] = [match.model_dump() for match in semantic_matches] if semantic_matches else []
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
) -> None:
    """Extract failure evidence from and analyze a local raw CI log."""

    result = analyze_log(raw_log_file.read_text(encoding="utf-8"))
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    _print_report(result)
