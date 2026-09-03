"""Command-line interface for local CIRCA analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .analyzer import analyze_failure
from .llm import classify_failure, explain_analysis
from .models import AnalysisResult, CIFailure, LLMClassification
from .pipeline import analyze_log

app = typer.Typer(help="Analyze CI failures using local deterministic evidence.")


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


@app.command()
def analyze(
    failure_json: Annotated[Path, typer.Argument(exists=True, readable=True, help="Path to a CIFailure JSON file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print the analysis result as JSON.")] = False,
    llm: Annotated[bool, typer.Option("--llm", help="Add an optional local Ollama explanation.")] = False,
    llm_classify: Annotated[
        bool, typer.Option("--llm-classify", help="Add an optional independent local LLM classification.")
    ] = False,
) -> None:
    """Analyze a local CIFailure JSON file."""

    failure = CIFailure.model_validate_json(failure_json.read_text(encoding="utf-8"))
    result = analyze_failure(failure)
    if llm:
        result = explain_analysis(failure.failure_text, result)
    classification = classify_failure(failure, result) if llm_classify else None
    if llm_classify and classification is None:
        typer.echo("Warning: LLM classification unavailable.", err=True)
    if json_output:
        if llm_classify:
            typer.echo(
                json.dumps(
                    {"deterministic_analysis": result.model_dump(), "llm_classification": classification.model_dump() if classification else None},
                    indent=2,
                )
            )
        else:
            typer.echo(result.model_dump_json(indent=2))
        return
    _print_report(result, "Deterministic Analysis" if llm_classify else "CIRCA Analysis Report")
    if classification:
        typer.echo()
        _print_llm_classification(classification)


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
