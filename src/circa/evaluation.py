"""Repeatable evaluation of deterministic classification on labeled CI failures.

Evaluation data is never knowledge: cases are read from their own dataset, only the
packaged synthetic incidents are matched, and nothing is written.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .analyzer import analyze_failure
from .benchmark import load_evaluation_cases
from .knowledge import load_incidents
from .llm import LLMDiagnosis
from .models import AnalysisResult, CauseCategory, CIFailure, HistoricalIncident
from .pipeline import investigate_log
from .resources import data_path

CATEGORIES: tuple[CauseCategory, ...] = get_args(CauseCategory)
DEFAULT_DATASET = Path("evaluation/cases.json")


class ClassificationCase(BaseModel):
    """One labeled failure: a log (file or inline) or, for legacy cases, structured failure text."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: Literal["real", "synthetic", "legacy"]
    label_status: Literal["confirmed", "pending_review"]
    log_file: str | None = None
    failure_log: str | None = None
    failure_text: str | None = None
    expected_category: CauseCategory
    relevant_incident_ids: list[str] = Field(default_factory=list)
    expected_root_cause_terms: list[str] = Field(default_factory=list)
    label_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_input(self) -> ClassificationCase:
        if sum(value is not None for value in (self.log_file, self.failure_log, self.failure_text)) != 1:
            raise ValueError("exactly one of log_file, failure_log, or failure_text is required")
        if self.source == "synthetic" and self.label_status != "confirmed":
            raise ValueError("synthetic labels are confirmed by construction")
        return self


class CaseResult(BaseModel):
    """CIRCA's deterministic result for one case, compared with its labels."""

    id: str
    source: str
    label_status: str
    expected_category: CauseCategory
    predicted_category: CauseCategory
    correct: bool
    confidence: float
    root_cause: str
    matched_incident_ids: list[str]
    match_correct: bool | None = None  # None: no relevant incident is labeled
    root_cause_correct: bool | None = None  # None: no root-cause terms are labeled
    llm_called: bool | None = None  # None: not a log, so investigate-log does not apply
    investigation_outcome: str | None = None


class CategoryMetrics(BaseModel):
    category: CauseCategory
    support: int
    predicted: int
    precision: float | None
    recall: float | None


class Rate(BaseModel):
    """A count over an explicit denominator; ``rate`` is None when the denominator is zero."""

    count: int
    total: int
    rate: float | None


class ClassificationMetrics(BaseModel):
    cases: int
    accuracy: Rate = Field(description="Cases whose predicted category equals the expected category.")
    unknown: Rate = Field(description="Cases CIRCA classified as unknown.")
    false_positives: Rate = Field(description="Expected-unknown cases that CIRCA assigned a category.")
    wrong_category: Rate = Field(description="Non-unknown predictions whose category is wrong.")
    matching: Rate = Field(description="Cases with labeled relevant incidents whose first match is relevant.")
    root_cause: Rate = Field(description="Cases with labeled root-cause terms whose root cause contains them all.")
    llm_asked: Rate = Field(description="Log cases where investigate-log asked the optional LLM.")
    llm_asked_when_correct: int = Field(
        description="LLM requests on log cases that deterministic analysis already classified correctly (not unknown)."
    )
    per_category: list[CategoryMetrics]
    confusion: dict[str, dict[str, int]] = Field(description="expected category -> predicted category -> count")


class EvaluationReport(BaseModel):
    dataset: str
    groups: dict[str, ClassificationMetrics]
    errors: dict[str, int] = Field(description="Most common 'expected -> predicted' misclassifications.")
    cases: list[CaseResult]


class _RecordingProvider:
    """Stands in for the optional LLM: records that it was asked, and answers nothing."""

    model = "evaluation-recorder"

    def diagnose(self, prompt: str) -> LLMDiagnosis | None:
        return None


def _rate(count: int, total: int) -> Rate:
    return Rate(count=count, total=total, rate=count / total if total else None)


def load_classification_cases(
    path: str | Path, incidents: Sequence[HistoricalIncident], *, include_legacy: bool = True
) -> list[ClassificationCase]:
    """Load the dataset (and the legacy retrieval cases), validating IDs, files, and incident references."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read evaluation dataset {source}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise TypeError(f"Invalid evaluation dataset {source}: expected an object with a 'cases' list.")

    cases: list[ClassificationCase] = []
    if include_legacy:
        legacy = load_evaluation_cases(data_path("retrieval_evaluation.json"), incidents)
        cases += [
            ClassificationCase(
                id=case.id,
                source="legacy",
                label_status="confirmed",
                failure_text=case.failure_text,
                expected_category=case.expected_category,
                relevant_incident_ids=case.relevant_incident_ids,
                label_note=case.relevance_note or "Synthetic retrieval evaluation case.",
            )
            for case in legacy
        ]
    known_ids = {incident.id for incident in incidents}
    root = source.parent.resolve()
    for index, record in enumerate(payload["cases"]):
        try:
            case = ClassificationCase.model_validate(record)
        except ValidationError as error:
            raise ValueError(f"Invalid evaluation case at index {index} in {source}: {error}") from error
        unknown_ids = sorted(set(case.relevant_incident_ids) - known_ids)
        if unknown_ids:
            raise ValueError(
                f"Evaluation case '{case.id}' references unknown incident ID(s): {', '.join(unknown_ids)}."
            )
        if case.log_file is not None:
            log_path = (root / case.log_file).resolve()
            if not log_path.is_relative_to(root) or not log_path.is_file():
                raise ValueError(f"Evaluation case '{case.id}' log file must exist inside {root}: {case.log_file}")
        cases.append(case)
    duplicates = sorted(identifier for identifier, count in Counter(case.id for case in cases).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate evaluation case ID(s): {', '.join(duplicates)}.")
    return cases


def _evaluate_case(case: ClassificationCase, root: Path, incidents: list[HistoricalIncident]) -> CaseResult:
    llm_called: bool | None = None
    outcome: str | None = None
    analysis: AnalysisResult
    if case.failure_text is not None:
        # Structured failure text, as `circa analyze` receives it.
        analysis = analyze_failure(CIFailure(failure_text=case.failure_text), incidents)
    else:
        raw_log = case.failure_log
        if raw_log is None:
            raw_log = (root / str(case.log_file)).read_text(encoding="utf-8", errors="replace")
        # The `circa investigate-log` path without team knowledge; the recorder shows whether the LLM is asked.
        investigation = investigate_log(raw_log, [], _RecordingProvider(), source_name=case.id)
        analysis, llm_called, outcome = investigation.analysis, investigation.llm_called, investigation.outcome

    terms = [term.lower() for term in case.expected_root_cause_terms]
    return CaseResult(
        id=case.id,
        source=case.source,
        label_status=case.label_status,
        expected_category=case.expected_category,
        predicted_category=analysis.category,
        correct=analysis.category == case.expected_category,
        confidence=analysis.confidence,
        root_cause=analysis.root_cause,
        matched_incident_ids=analysis.matched_incident_ids,
        match_correct=(
            bool(analysis.matched_incident_ids) and analysis.matched_incident_ids[0] in case.relevant_incident_ids
            if case.relevant_incident_ids
            else None
        ),
        root_cause_correct=all(term in analysis.root_cause.lower() for term in terms) if terms else None,
        llm_called=llm_called,
        investigation_outcome=outcome,
    )


def calculate_classification_metrics(results: Sequence[CaseResult]) -> ClassificationMetrics:
    """Summarize case results; every rate carries its count and denominator."""

    confusion = {expected: dict.fromkeys(CATEGORIES, 0) for expected in CATEGORIES}
    for result in results:
        confusion[result.expected_category][result.predicted_category] += 1
    per_category = []
    for category in CATEGORIES:
        support = sum(confusion[category].values())
        predicted = sum(row[category] for row in confusion.values())
        hits = confusion[category][category]
        per_category.append(
            CategoryMetrics(
                category=category,
                support=support,
                predicted=predicted,
                precision=hits / predicted if predicted else None,
                recall=hits / support if support else None,
            )
        )
    expected_unknown = [result for result in results if result.expected_category == "unknown"]
    categorized = [result for result in results if result.predicted_category != "unknown"]
    matched = [result.match_correct for result in results if result.match_correct is not None]
    root_causes = [result.root_cause_correct for result in results if result.root_cause_correct is not None]
    logs = [result for result in results if result.llm_called is not None]
    return ClassificationMetrics(
        cases=len(results),
        accuracy=_rate(sum(result.correct for result in results), len(results)),
        unknown=_rate(len(results) - len(categorized), len(results)),
        false_positives=_rate(
            sum(result.predicted_category != "unknown" for result in expected_unknown), len(expected_unknown)
        ),
        wrong_category=_rate(sum(not result.correct for result in categorized), len(categorized)),
        matching=_rate(sum(matched), len(matched)),
        root_cause=_rate(sum(root_causes), len(root_causes)),
        llm_asked=_rate(sum(bool(result.llm_called) for result in logs), len(logs)),
        llm_asked_when_correct=sum(
            bool(result.llm_called) and result.correct and result.predicted_category != "unknown" for result in logs
        ),
        per_category=per_category,
        confusion=confusion,
    )


def run_classification_evaluation(
    path: str | Path = DEFAULT_DATASET, *, include_legacy: bool = True
) -> EvaluationReport:
    """Evaluate every case deterministically against the packaged incidents only."""

    incidents = load_incidents(data_path("incidents.json"))
    cases = load_classification_cases(path, incidents, include_legacy=include_legacy)
    root = Path(path).parent.resolve()
    results = [_evaluate_case(case, root, incidents) for case in cases]

    def group(name: str) -> list[CaseResult]:
        if name == "all_confirmed":
            return [result for result in results if result.label_status == "confirmed"]
        if name == "pending_review":
            return [result for result in results if result.label_status == "pending_review"]
        return [result for result in results if result.source == name and result.label_status == "confirmed"]

    names = ["all_confirmed", "legacy", "synthetic", "real", "pending_review"]
    errors = Counter(
        f"{result.expected_category} -> {result.predicted_category}" for result in results if not result.correct
    )
    return EvaluationReport(
        dataset=str(path),
        groups={name: calculate_classification_metrics(group(name)) for name in names},
        errors=dict(errors.most_common()),
        cases=results,
    )
