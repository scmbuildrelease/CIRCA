"""Command-line interface for local CIRCA analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .analyzer import analyze_failure
from .models import AnalysisResult, CIFailure

app = typer.Typer(help="Analyze CI failures using local deterministic evidence.")


@app.callback()
def main() -> None:
    """CIRCA command group."""


def _print_report(result: AnalysisResult) -> None:
    """Print one concise, human-readable analysis report."""

    typer.echo("CIRCA Analysis Report")
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


@app.command()
def analyze(
    failure_json: Annotated[Path, typer.Argument(exists=True, readable=True, help="Path to a CIFailure JSON file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print the analysis result as JSON.")] = False,
) -> None:
    """Analyze a local CIFailure JSON file."""

    failure = CIFailure.model_validate_json(failure_json.read_text(encoding="utf-8"))
    result = analyze_failure(failure)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    _print_report(result)
