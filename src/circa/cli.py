"""Command-line interface for local CIRCA analysis."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from typer.core import TyperGroup

from .analyzer import analyze_failure
from .benchmark import BenchmarkMethodResult, load_evaluation_cases, run_retrieval_benchmark
from .doctor import DEFAULT_KNOWLEDGE, Check, circa_version, run_checks
from .evaluation import DEFAULT_DATASET, ClassificationMetrics, EvaluationReport, Rate, run_classification_evaluation
from .github_runs import (
    MAX_RUN_LIMIT,
    FailedRun,
    GitHubRunError,
    detect_repository,
    fetch_failed_log,
    is_repository_url,
    is_run_reference,
    list_failed_runs,
    parse_repository,
    parse_run_reference,
)
from .hybrid import apply_hybrid_scoring
from .knowledge import load_incidents
from .knowledge_store import (
    MANAGED_FIELDS,
    DuplicateIncidentError,
    KnowledgeError,
    KnowledgeFile,
    add_incident,
    load_analysis_incidents,
    load_knowledge,
    reject_incident,
    save_knowledge,
    serialize_incidents,
    supersede_incident,
    update_incident,
    validation_message,
    verify_incident,
)
from .llm import OllamaProvider, classify_failure, explain_analysis
from .models import AnalysisResult, CIFailure, HistoricalIncident, LLMClassification, SemanticIncidentMatch
from .pipeline import Investigation, analyze_log, investigate_log
from .redaction import RedactionError, redact_secrets
from .resources import data_path
from .semantic import OllamaEmbeddingProvider, SemanticIncidentRetriever

# Terminal control sequences in CI logs (and other untrusted text) could recolor, retitle, or
# rewrite the terminal. Human-readable output drops them; analysis input and --json output keep
# the original text, since JSON escapes control characters.
_TERMINAL_SEQUENCE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colors, cursor movement, erasing
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: window title, hyperlinks, clipboard
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"  # DCS, SOS, PM, APC strings
    r"|\x1b[ -/]*[0-~]"  # other escape sequences
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
# C0/C1 controls except tab and newline (a carriage return can overwrite a line), and bidi overrides.
_UNSAFE_CHARACTER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def safe_text(text: str) -> str:
    """Return ``text`` without terminal control sequences or recognized credentials.

    Printable Unicode, tabs, and newlines are kept.
    """

    return redact_secrets(_UNSAFE_CHARACTER.sub("", _TERMINAL_SEQUENCE.sub("", text)))


def _echo(message: str = "", err: bool = False) -> None:
    typer.echo(safe_text(message), err=err)


def _echo_json(document: str) -> None:
    # JSON escapes control characters itself; only credentials are redacted.
    typer.echo(redact_secrets(document))


class _FailClosedGroup(TyperGroup):
    """Stop any command whose input or output could not be redacted, without showing either."""

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except RedactionError as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(1) from None


app = typer.Typer(
    cls=_FailClosedGroup,
    pretty_exceptions_show_locals=False,  # local variables can hold log text or credentials
    help="Analyze CI failures using local deterministic evidence.",
    epilog="Start here: circa doctor, then circa init. "
    "Then: circa runs (list failed GitHub Actions runs), "
    "circa investigate-log --knowledge team/incidents.json (choose one), "
    "or circa investigate-log path/to/ci.log (a local log).",
)
knowledge_app = typer.Typer(help="Manage a human-reviewed team incident knowledge file.")
app.add_typer(knowledge_app, name="knowledge")

_HYBRID_FIELDS = {"llm_classification", "final_category", "final_confidence", "final_root_cause"}


def _show_version(value: bool) -> None:
    if value:
        _echo(f"circa {circa_version()}")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show the CIRCA version and exit."),
    ] = False,
) -> None:
    """CIRCA command group."""


def _print_report(result: AnalysisResult, heading: str = "CIRCA Analysis Report") -> None:
    """Print one concise, human-readable analysis report."""

    _echo(heading)
    _echo(f"Category: {result.category}")
    _echo(f"Confidence: {result.confidence:.2f}")
    _echo(f"Root cause: {result.root_cause}")
    _echo("Evidence:")
    if result.evidence:
        for item in result.evidence:
            _echo(f"- {item}")
    else:
        _echo("- No strong deterministic evidence.")
    _echo("Historical matches:")
    if result.matched_incident_ids:
        for incident_id in result.matched_incident_ids:
            _echo(f"- {incident_id}")
    else:
        _echo("- None")
    _echo(f"Recommendation: {result.recommendation}")
    _echo(f"Owner: {result.owner}")
    _echo(f"Fingerprint: {result.fingerprint}")
    if result.llm_explanation:
        _echo(f"LLM explanation: {result.llm_explanation}")
    if result.performance_assessment:
        assessment = result.performance_assessment
        _echo("Performance Assessment")
        _echo(f"Failure type: {assessment.failure_type}")
        _echo(f"Candidate cause (hypothesis): {assessment.candidate_cause}")
        _echo(f"Recommended action: {assessment.recommended_action or 'none'}")
        for measurement in assessment.measurements:
            _echo(
                f"Measurement: {measurement.metric}={measurement.actual_value:g} {measurement.unit or ''} "
                f"(expected {measurement.expected_range})"
            )
        if assessment.missing_evidence:
            _echo("Missing performance evidence:")
            for item in assessment.missing_evidence:
                _echo(f"- {item}")
        _echo(f"Performance recommendation: {assessment.recommendation}")


def _print_llm_classification(classification: LLMClassification) -> None:
    """Print the independent LLM classification separately from deterministic output."""

    _echo("LLM Classification")
    _echo(f"Category: {classification.category}")
    _echo(f"Confidence: {classification.confidence:.2f}")
    _echo(f"Root Cause: {classification.root_cause}")
    _echo("Evidence:")
    if classification.evidence:
        for item in classification.evidence:
            _echo(f"- {item}")
    else:
        _echo("- None")


def _print_hybrid_analysis(result: AnalysisResult) -> None:
    """Print the final deterministic-first hybrid result."""

    _echo("Final Hybrid Analysis")
    _echo(f"Category: {result.final_category}")
    _echo(f"Confidence: {result.final_confidence:.2f}")
    _echo(f"Root Cause: {result.final_root_cause}")


def _print_semantic_matches(matches: list[SemanticIncidentMatch]) -> None:
    """Print ranked local semantic incident matches."""

    _echo("Semantic Historical Matches")
    if not matches:
        _echo("- None above the similarity threshold.")
        return
    for match in matches:
        _echo(f"- {match.incident.id}: {match.similarity:.2f}")


def _format_metric(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.3f}"


def _print_benchmark_method(method: BenchmarkMethodResult, top_k: int) -> None:
    """Print retrieval-only metrics and their documented denominators."""

    metrics = method.metrics
    _echo(f"{method.name.title()} retrieval ({method.status})")
    _echo(f"  evaluated: {metrics.evaluated_cases}; skipped: {metrics.skipped_cases}")
    _echo(f"  relevant-label cases: {metrics.relevant_cases}; no-known-relevance: {metrics.no_known_relevance_cases}")
    _echo(f"  Top-1 accuracy (relevant-label cases): {_format_metric(metrics.top_1_accuracy)}")
    _echo(f"  Recall@{top_k} (relevant-label cases): {_format_metric(metrics.recall_at_k)}")
    _echo(f"  MRR (relevant-label cases): {_format_metric(metrics.mean_reciprocal_rank)}")
    _echo(f"  no-result rate (evaluated cases): {_format_metric(metrics.no_result_rate)}")
    _echo(f"  coverage (all dataset cases): {_format_metric(metrics.evaluation_coverage)}")


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
        _echo(f"Error: {error}", err=True)
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

    try:
        failure = CIFailure.model_validate_json(failure_json.read_text(encoding="utf-8"))
    except ValidationError as error:
        # Pydantic's own message quotes input values, which may hold credentials.
        raise _fail(ValueError(f"invalid CIFailure JSON: {validation_message(error)}")) from error
    except (OSError, UnicodeDecodeError) as error:
        raise _fail(error) from error
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
        _echo("Warning: LLM classification unavailable.", err=True)
    if semantic_retrieve and semantic_matches is None:
        _echo("Warning: semantic retrieval unavailable.", err=True)
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
            _echo_json(json.dumps(payload, indent=2))
        else:
            payload = dict(result.model_dump(exclude=_HYBRID_FIELDS))
            if semantic_retrieve:
                payload["semantic_matches"] = (
                    [match.model_dump() for match in semantic_matches] if semantic_matches is not None else None
                )
            _echo_json(json.dumps(payload, indent=2))
        return
    _print_report(result, "Deterministic Analysis" if llm_classify else "CIRCA Analysis Report")
    if classification:
        _echo()
        _print_llm_classification(classification)
        _echo()
        _print_hybrid_analysis(result)
    if semantic_retrieve and semantic_matches is not None:
        _echo()
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
        _echo_json(result.model_dump_json(indent=2))
        return
    _print_report(result)


def _print_incident(incident: HistoricalIncident) -> None:
    """Print a team incident's diagnosis, evidence, fix, and provenance for review."""

    provenance = "synthetic" if incident.synthetic else "real"
    _echo(f"Incident {incident.id} ({incident.status}, {provenance}, {incident.category})")
    _echo(f"Title: {incident.title}")
    _echo(f"Root cause: {incident.root_cause or incident.summary}")
    _echo("Evidence:")
    for line in incident.evidence or ["None recorded."]:
        _echo(f"- {line}")
    if incident.fix:
        _echo(f"Fix: {incident.fix.description}")
        _echo(f"Fix validation: {incident.fix.validation_status}")
        if incident.fix.validation_method:
            _echo(f"Validation evidence ({incident.fix.validation_method}): {incident.fix.validation_evidence}")
    else:
        _echo(f"Recommendation: {incident.recommendation}")
    if incident.reviewed_by:
        _echo(f"Reviewed by: {incident.reviewed_by}")
    origin = incident.draft_origin
    if origin:
        model = origin.llm_model or "unknown model"
        _echo(f"Origin: AI-generated hypothesis ({model}) from {origin.source_log}")
        _echo(f"AI confidence: {origin.confidence:.2f}")
        for step in origin.validation_steps:
            _echo(f"Validation step: {step}")
        for item in origin.missing_evidence:
            _echo(f"Missing evidence: {item}")
        _echo(f"Related incidents: {', '.join(origin.related_incident_ids) or 'none'}")
        if origin.category_source:
            _echo(f"Category source: {origin.category_source}")


def _save_draft(path: Path, draft: dict[str, Any]) -> None:
    """Add an investigation draft as pending_review; duplicates are reported, not written."""

    try:
        current = load_knowledge(path, must_exist=False)
        incidents, incident, _ = add_incident(current, draft)
        save_knowledge(current, incidents)
    except DuplicateIncidentError as error:
        _echo(f"Draft not saved: {error}", err=True)
        return
    except (KnowledgeError, OSError) as error:
        raise _fail(KnowledgeError(f"draft not saved: {error}")) from error
    # Status goes to stderr like other notes, so --json output stays parseable.
    _echo(f"Draft saved: {incident.id} (pending_review) in {path}", err=True)
    _echo(f"Review it: circa knowledge show {incident.id} --knowledge {path}", err=True)


def _print_investigation(investigation: Investigation) -> None:
    _print_report(investigation.analysis, "CIRCA Investigation")
    _echo(f"Outcome: {investigation.outcome}")
    _echo(f"LLM called: {'yes' if investigation.llm_called else 'no'}")
    incident = investigation.matched_incident
    if investigation.outcome == "known_solution" and incident is not None:
        _echo()
        _echo(f"Known verified solution (matched by {investigation.match_reason}):")
        _print_incident(incident)
        _echo("Note: the fix was validated for the recorded incident; confirm it applies to this environment.")
    elif incident is not None:
        _echo()
        _echo(f"Existing team record (matched by {investigation.match_reason}):")
        _print_incident(incident)
    diagnosis = investigation.diagnosis
    if diagnosis is not None:
        _echo()
        _echo("AI diagnosis (hypothesis from the local LLM; not verified)")
        _echo(f"Category: {diagnosis.category}")
        _echo(f"Confidence: {diagnosis.confidence:.2f}")
        _echo(f"Candidate root cause: {diagnosis.root_cause}")
        _echo(f"Suggested fix: {diagnosis.suggested_fix or 'none'}")
        for step in diagnosis.validation_steps:
            _echo(f"Validation step: {step}")
        for item in diagnosis.missing_evidence:
            _echo(f"Missing evidence: {item}")
    if investigation.evidence:
        _echo("Observed log evidence:")
        for line in investigation.evidence:
            _echo(f"- {line}")
    for note in investigation.notes:
        _echo(f"Note: {note}", err=True)


_RunLimit = Annotated[
    int,
    typer.Option(
        "--limit", min=1, max=MAX_RUN_LIMIT, help=f"Number of recent failed runs to list (1-{MAX_RUN_LIMIT})."
    ),
]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _print_failed_runs(runs: list[FailedRun], numbered: bool = False) -> None:
    # Printed to stderr when selecting, so --json output on stdout stays one JSON document.
    for index, run in enumerate(runs, start=1):
        prefix = f"{index:>2}. " if numbered else ""
        fields = (run.run_id, run.workflow, run.branch, run.conclusion, run.created_at, run.url)
        _echo(prefix + "\t".join(fields), err=numbered)


def _detected_repository() -> str:
    """Return the repository of the current Git checkout, saying which remote it came from."""

    repository, remotes = detect_repository()
    _echo(f"Using {repository} (from git remote {', '.join(remotes)}); pass OWNER/REPO to choose another.", err=True)
    return repository


def _select_run(repository: str, limit: int) -> tuple[str, str]:
    """List the repository's failed runs, let the user pick one, and fetch its failed-step logs."""

    try:
        repository = parse_repository(repository)
    except GitHubRunError as error:
        raise _fail(error) from error
    if not _interactive():
        raise _fail(
            GitHubRunError(
                f"no run selected and no terminal to choose one; list runs with 'circa runs {repository}', "
                f"then pass one: circa investigate-log RUN_ID --repo {repository}"
            )
        )
    try:
        runs = list_failed_runs(repository, limit)
    except GitHubRunError as error:
        raise _fail(error) from error
    _echo(f"Recent failed runs in {repository} (run ID, workflow, branch, conclusion, created, URL):", err=True)
    _print_failed_runs(runs, numbered=True)

    def run_number(text: str) -> int:
        if not text.strip().isdigit() or not 1 <= int(text) <= len(runs):
            raise typer.BadParameter(f"enter a number from 1 to {len(runs)}")
        return int(text)

    choice = typer.prompt(f"Select a run [1-{len(runs)}]", value_proc=run_number, err=True)
    return _read_log_source(runs[choice - 1].run_id, repository)


def _read_log_source(log_source: str, repository: str | None) -> tuple[str, str]:
    """Return the log text and its source name for a local file or a GitHub Actions run."""

    if is_run_reference(log_source) and not Path(log_source).is_file():
        try:
            run = parse_run_reference(log_source, repository)
            return fetch_failed_log(run), run.url
        except GitHubRunError as error:
            raise _fail(error) from error
    if repository is not None and not Path(log_source).is_file():
        raise _fail(GitHubRunError(f"'{log_source}' is neither a local file nor a numeric GitHub Actions run ID"))
    if repository is not None:
        raise _fail(GitHubRunError("--repo applies only to a GitHub Actions run ID"))
    path = Path(log_source)
    if not path.is_file():
        raise typer.BadParameter(f"File '{log_source}' does not exist or is not a file.", param_hint="'LOG_SOURCE'")
    try:
        return path.read_text(encoding="utf-8"), path.name
    except (OSError, UnicodeDecodeError) as error:
        raise _fail(error) from error


@app.command("investigate-log")
def investigate_log_file(
    log_source: Annotated[
        str | None,
        typer.Argument(
            help="A raw CI log file, a GitHub Actions run URL (https://github.com/OWNER/REPO/actions/runs/ID), "
            "or a run ID with --repo. Runs are fetched with 'gh run view --log-failed'. Omit it (or pass "
            "https://github.com/OWNER/REPO) to choose from the repository's recent failed runs in a terminal.",
            show_default=False,
        ),
    ] = None,
    knowledge: Annotated[
        Path | None,
        typer.Option(
            "--knowledge",
            dir_okay=False,
            help="Team knowledge file: verified incidents are matched; new diagnoses are added as pending_review drafts.",
        ),
    ] = None,
    synthetic: Annotated[
        bool, typer.Option("--synthetic", help="Mark any new draft as synthetic (demo or test data).")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the investigation as JSON.")] = False,
    repository: Annotated[
        str | None,
        typer.Option("--repo", help="OWNER/REPO for a numeric run ID, or to choose a failed run.", show_default=False),
    ] = None,
    limit: _RunLimit = 10,
) -> None:
    """Investigate a CI log or GitHub Actions run: verified history first, then the optional local LLM, then a draft."""

    if log_source is not None and is_repository_url(log_source) and not Path(log_source).is_file():
        try:
            if repository is not None and parse_repository(log_source).lower() != parse_repository(repository).lower():
                raise GitHubRunError(f"--repo {repository} does not match the repository URL {log_source}")
        except GitHubRunError as error:
            raise _fail(error) from error
        log_source, repository = None, log_source
    if log_source is None:
        if repository is None:
            try:
                repository = _detected_repository()
            except GitHubRunError as error:
                raise typer.BadParameter(
                    f"pass a log file, a run URL, or --repo OWNER/REPO ({error}).", param_hint="'LOG_SOURCE'"
                ) from error
        raw_log, source_name = _select_run(repository, limit)
    else:
        if repository is None and log_source.isascii() and log_source.isdigit() and not Path(log_source).is_file():
            try:
                repository = _detected_repository()
            except GitHubRunError as error:
                raise _fail(GitHubRunError(f"run ID {log_source} needs --repo OWNER/REPO: {error}")) from error
        raw_log, source_name = _read_log_source(log_source, repository)
    try:
        team = load_knowledge(knowledge, must_exist=False).incidents if knowledge is not None else []
    except KnowledgeError as error:
        raise _fail(error) from error
    # Generating a diagnosis takes longer than an explanation; an absent server still fails fast.
    investigation = investigate_log(
        raw_log, team, OllamaProvider(timeout=60.0), source_name=source_name, synthetic=synthetic
    )
    if json_output:
        _echo_json(investigation.model_dump_json(indent=2, exclude_none=True))
    else:
        _print_investigation(investigation)
    if investigation.draft is None:
        return
    if knowledge is None:
        _echo("Draft not saved: pass --knowledge PATH to keep new diagnoses for review.", err=True)
        return
    _save_draft(knowledge, investigation.draft)


@app.command()
def runs(
    repository: Annotated[
        str | None,
        typer.Argument(
            help="OWNER/REPO or https://github.com/OWNER/REPO; detected from the Git remote when omitted.",
            show_default=False,
        ),
    ] = None,
    limit: _RunLimit = 10,
    json_output: Annotated[bool, typer.Option("--json", help="Print the runs as a JSON list.")] = False,
) -> None:
    """List a repository's recent failed GitHub Actions runs (via 'gh run list'); nothing is analyzed or saved."""

    try:
        repository = repository or _detected_repository()
        failed = list_failed_runs(repository, limit)
    except GitHubRunError as error:
        raise _fail(error) from error
    if json_output:
        _echo_json(json.dumps([run.as_dict() for run in failed], indent=2))
        return
    _echo("RUN ID\tWORKFLOW\tBRANCH\tCONCLUSION\tCREATED\tURL")
    _print_failed_runs(failed)
    _echo(f"Investigate one with: circa investigate-log RUN_ID --repo {parse_repository(repository)}", err=True)


_STATUS_LABEL = {"ok": "ok", "optional": "optional", "error": "ERROR"}


def _print_checks(checks: list[Check]) -> None:
    for check in checks:
        _echo(f"{_STATUS_LABEL[check.status]:<9} {check.name}: {check.detail}")


@app.command()
def doctor(
    knowledge: Annotated[
        Path | None,
        typer.Option("--knowledge", dir_okay=False, help="Team knowledge file to check.", show_default=False),
    ] = None,
) -> None:
    """Check Python, Git, GitHub CLI login, the repository, the knowledge file, and Ollama. Changes nothing."""

    checks = run_checks(knowledge)
    _print_checks(checks)
    if any(check.status == "error" for check in checks):
        raise typer.Exit(1)
    _echo("CIRCA is ready for local logs. Items marked optional are needed only for GitHub runs or AI drafts.")


@app.command()
def init(
    knowledge: Annotated[
        Path, typer.Option("--knowledge", dir_okay=False, help="Team knowledge file to create if it does not exist.")
    ] = DEFAULT_KNOWLEDGE,
) -> None:
    """Check prerequisites and create an empty knowledge file if none exists. Safe to run again; never overwrites."""

    created = False
    if not knowledge.exists():
        try:
            knowledge.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _fail(KnowledgeError(f"could not create {knowledge}: {error.strerror or error}")) from error
        try:
            # "x" mode fails rather than overwrite a file created in the meantime.
            with knowledge.open("x", encoding="utf-8") as handle:
                handle.write(serialize_incidents([]))
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise _fail(KnowledgeError(f"could not create {knowledge}: {error.strerror or error}")) from error
    checks = run_checks(knowledge)
    _print_checks(checks)
    if created:
        _echo(f"Created an empty knowledge file: {knowledge}")
    else:
        _echo(f"Using the existing knowledge file {knowledge}; it was not changed.")
    if any(check.status == "error" for check in checks):
        raise typer.Exit(1)
    status = {check.name: check for check in checks}
    repository = status["Repository"].detail.split()[0] if status["Repository"].status == "ok" else None
    repo_argument = "" if repository else " OWNER/REPO"
    repo_option = "" if repository else " --repo OWNER/REPO"
    steps = [
        ("List recent failed GitHub Actions runs:", f"circa runs{repo_argument}"),
        (
            "Choose one and investigate it (in a terminal):",
            f"circa investigate-log{repo_option} --knowledge {knowledge}",
        ),
        ("Or investigate a local log file:", f"circa investigate-log path/to/ci.log --knowledge {knowledge}"),
    ]
    if status.get("GitHub login", status["GitHub CLI"]).status != "ok":
        steps.insert(0, ("Log in to GitHub once (needed only for GitHub runs):", "gh auth login"))
    _echo()
    _echo("Next steps:")
    for description, command in steps:
        _echo(f"  {description}")
        _echo(f"    {command}")


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
    _echo(f"CIRCA Retrieval Benchmark: {report.dataset_size} dataset cases; Top-K: {report.top_k}")
    for method in report.methods:
        _echo()
        _print_benchmark_method(method, report.top_k)


def _format_rate(rate: Rate) -> str:
    return f"{rate.count}/{rate.total} ({rate.rate:.1%})" if rate.rate is not None else "n/a (no cases)"


def _print_evaluation_group(name: str, metrics: ClassificationMetrics) -> None:
    """Print one group's metrics; each states its own denominator."""

    _echo(f"{name}: {metrics.cases} case(s)")
    if not metrics.cases:
        return
    _echo(f"  category accuracy: {_format_rate(metrics.accuracy)}")
    _echo(f"  classified unknown: {_format_rate(metrics.unknown)}")
    _echo(f"  false positives (expected unknown, got a category): {_format_rate(metrics.false_positives)}")
    _echo(f"  wrong category (of non-unknown predictions): {_format_rate(metrics.wrong_category)}")
    _echo(f"  first historical match relevant (labeled cases): {_format_rate(metrics.matching)}")
    _echo(f"  root cause contains the labeled terms: {_format_rate(metrics.root_cause)}")
    if metrics.llm_asked.total:
        _echo(
            f"  LLM asked (log cases): {_format_rate(metrics.llm_asked)};"
            f" of those, already correct deterministically: {metrics.llm_asked_when_correct}"
        )


def _print_evaluation(report: EvaluationReport) -> None:
    _echo(f"CIRCA Classification Evaluation: {len(report.cases)} case(s) from {report.dataset} and the legacy set")
    _echo("Deterministic only; packaged synthetic incidents only; nothing is written.")
    titles = {
        "all_confirmed": "All confirmed labels",
        "legacy": "Legacy synthetic cases (baseline)",
        "synthetic": "Synthetic log cases",
        "real": "Real logs (confirmed labels)",
        "pending_review": "Real logs (labels pending review; not in the totals above)",
    }
    for name, metrics in report.groups.items():
        _echo()
        _print_evaluation_group(titles.get(name, name), metrics)
    _echo()
    _echo("Per category (all confirmed labels): support, precision, recall")
    for row in report.groups["all_confirmed"].per_category:
        _echo(f"  {row.category}: {row.support}, {_format_metric(row.precision)}, {_format_metric(row.recall)}")
    _echo()
    _echo("Misclassifications, all cases (expected -> predicted):")
    if not report.errors:
        _echo("  none")
    for pattern, count in report.errors.items():
        _echo(f"  {pattern}: {count}")
    pending = {case.id for case in report.cases if case.label_status == "pending_review"}
    for case in report.cases:
        if not case.correct:
            note = " (label pending review)" if case.id in pending else ""
            _echo(f"  - {case.id}: {case.expected_category} -> {case.predicted_category}{note}")
    _echo()
    _echo("Root cause missing the labeled terms:")
    misses = [case for case in report.cases if case.root_cause_correct is False]
    if not misses:
        _echo("  none")
    for case in misses:
        note = " (label pending review)" if case.id in pending else ""
        _echo(f"  - {case.id} ({case.predicted_category}){note}: {case.root_cause}")


@app.command()
def evaluate(
    dataset: Annotated[
        Path, typer.Option("--dataset", dir_okay=False, help="Classification evaluation dataset JSON file.")
    ] = DEFAULT_DATASET,
    json_output: Annotated[bool, typer.Option("--json", help="Print the full evaluation as JSON.")] = False,
) -> None:
    """Evaluate deterministic classification on labeled cases (never reads or writes team knowledge)."""

    try:
        report = run_classification_evaluation(dataset)
    except (TypeError, ValueError) as error:
        raise _fail(error) from error
    if json_output:
        _echo_json(report.model_dump_json(indent=2))
        return
    _print_evaluation(report)


KnowledgeFileOption = Annotated[
    Path, typer.Option("--knowledge", dir_okay=False, help="Team knowledge JSON file to read and update.")
]
YesOption = Annotated[bool, typer.Option("--yes", help="Write without the interactive confirmation prompt.")]
AllowSimilarOption = Annotated[
    bool, typer.Option("--allow-similar", help="Proceed even when a near-duplicate incident is reported.")
]


def _fail(error: Exception) -> typer.Exit:
    _echo(f"Error: {error}", err=True)
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

    _echo(f"{action} incident '{incident.id}' ({incident.status}) in {knowledge.path}:")
    _echo(serialize_incidents([incident]).strip())
    if not yes and not typer.confirm("Write this change?", default=False):
        _echo("Aborted; nothing was written.", err=True)
        raise typer.Exit(1)
    try:
        save_knowledge(knowledge, incidents)
    except KnowledgeError as error:
        raise _fail(error) from error
    _echo(f"Wrote {knowledge.path}")


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
        _echo(f"Warning: {warning}", err=True)
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
        _echo(f"Warning: {warning}", err=True)
    _confirm_and_save(current, incidents, incident, "Update", yes)


@knowledge_app.command("verify")
def knowledge_verify(
    incident_id: Annotated[str, typer.Argument(help="ID of a pending_review incident.")],
    knowledge: KnowledgeFileOption,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Name of the human reviewer approving it.")],
    validation_method: Annotated[
        str | None,
        typer.Option(
            "--validation-method",
            help="How you validated the fix: reproduced_then_fixed, regression_test_added, "
            "repeated_runs_passed, or manual_verification.",
        ),
    ] = None,
    validation_evidence: Annotated[
        str | None, typer.Option("--validation-evidence", help="What actually showed the fix works.")
    ] = None,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            help="Confirm or correct the category (hardware, software, network, infrastructure, unknown); "
            "required when the LLM chose it.",
        ),
    ] = None,
    yes: YesOption = False,
) -> None:
    """Approve a pending_review incident. Its fix is validated only with your own validation evidence."""

    if (validation_method is None) != (validation_evidence is None):
        raise _fail(KnowledgeError("--validation-method and --validation-evidence must be given together"))
    validation = (validation_method, validation_evidence) if validation_method and validation_evidence else None
    try:
        current = load_knowledge(knowledge)
        incidents, incident = verify_incident(
            current, incident_id, reviewer=reviewer, validation=validation, category=category
        )
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


@knowledge_app.command("reject")
def knowledge_reject(
    incident_id: Annotated[str, typer.Argument(help="ID of a pending_review incident.")],
    knowledge: KnowledgeFileOption,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Name of the human reviewer rejecting it.")],
    yes: YesOption = False,
) -> None:
    """Archive a pending_review draft as rejected; it is kept but never matched."""

    try:
        current = load_knowledge(knowledge)
        incidents, incident = reject_incident(current, incident_id, reviewer=reviewer)
    except KnowledgeError as error:
        raise _fail(error) from error
    _confirm_and_save(current, incidents, incident, "Reject", yes)


@knowledge_app.command("show")
def knowledge_show(
    incident_id: Annotated[str, typer.Argument(help="ID of a team incident.")],
    knowledge: KnowledgeFileOption,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print an editable draft for 'circa knowledge update'.")
    ] = False,
) -> None:
    """Show one incident for review, or export it as an editable draft."""

    try:
        incident = next((item for item in load_knowledge(knowledge).incidents if item.id == incident_id), None)
    except KnowledgeError as error:
        raise _fail(error) from error
    if incident is None:
        raise _fail(KnowledgeError(f"incident '{incident_id}' not found in {knowledge}"))
    if json_output:
        record = incident.model_dump(mode="json", exclude_none=True, exclude=set(MANAGED_FIELDS))
        _echo_json(json.dumps(record, indent=2, ensure_ascii=False))
        return
    _print_incident(incident)


@knowledge_app.command("list")
def knowledge_list(
    knowledge: KnowledgeFileOption,
    pending: Annotated[bool, typer.Option("--pending", help="Only list pending_review drafts.")] = False,
) -> None:
    """List team incidents with their status and provenance."""

    try:
        current = load_knowledge(knowledge)
    except KnowledgeError as error:
        raise _fail(error) from error
    for incident in sorted(current.incidents, key=lambda item: item.id):
        if pending and incident.status != "pending_review":
            continue
        provenance = "synthetic" if incident.synthetic else "real"
        _echo(f"{incident.id}\t{incident.status}\t{provenance}\t{incident.title}")


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
        for status in ("pending_review", "verified", "superseded", "rejected")
    }
    _echo(f"{knowledge_json}: valid; " + ", ".join(f"{count} {status}" for status, count in counts.items()))
