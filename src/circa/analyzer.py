"""Deterministic local CI failure analysis."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .fingerprint import failure_fingerprint, normalize_environment
from .knowledge import find_similar_incidents, incident_tokens, load_incidents, tokenize
from .models import AnalysisResult, CauseCategory, CIFailure, HistoricalIncident
from .resources import data_path

_OWNERS = {
    "hardware": "hardware-ops",
    "network": "network-ops",
    "infrastructure": "ci-ops",
    "software": "developer",
    "unknown": "unknown",
}
_CATEGORY_KEYWORDS: dict[CauseCategory, set[str]] = {
    "hardware": {"disk", "memory", "oom", "filesystem", "cpu"},
    "software": {"assertion", "assertionerror", "modulenotfounderror", "import", "regression", "traceback"},
    "network": {"timeout", "dns", "resolve", "connection", "hostname", "registry"},
    "infrastructure": {"runner", "image", "provisioning", "artifact", "503", "unavailable"},
    "unknown": set(),
}


# Words in nearly every CI or pytest log; sharing them with an incident is not evidence.
_GENERIC_TOKENS = frozenset(
    {"test", "tests", "process", "python", "module", "run", "job", "step", "exit", "code", "completed", "passed"}
)


@dataclass(frozen=True)
class _ExplicitError:
    """An error that names its own cause, such as an exception type in a traceback."""

    kind: str
    pattern: re.Pattern[str]
    # Error names, never ordinary words: an incident naming one describes this family of error.
    # ModuleNotFoundError is a subclass of ImportError, and pytest reports both, so they share a family.
    incident_tokens: frozenset[str]
    # The subset whose incidents may explain this error: the same error or a more general one,
    # never a more specific one (a ModuleNotFoundError incident does not explain "cannot import name").
    cause_tokens: frozenset[str]
    root_cause: str
    recommendation: str


# Ordered by pipeline stage: a dependency or import problem stops the code before
# any test runs, so it outranks an assertion reported in the same log.
_EXPLICIT_ERRORS = (
    _ExplicitError(
        "dependency installation failure",
        re.compile(
            r"No matching distribution found for|Could not find a version that satisfies the requirement"
            r"|ResolutionImpossible|npm ERR! code (?:ERESOLVE|ETARGET|E404)"
        ),
        frozenset({"resolutionimpossible", "eresolve", "etarget"}),
        frozenset({"resolutionimpossible", "eresolve", "etarget"}),
        "Dependency installation failed: the package manager could not install a required package or version.",
        "Check the requirement's name, version pin, and package index, and reproduce the install locally.",
    ),
    _ExplicitError(
        "missing module (ModuleNotFoundError)",
        re.compile(r"(?<!except )\bModuleNotFoundError(?::| while)|\bNo module named ['\"]"),
        frozenset({"modulenotfounderror", "importerror"}),
        frozenset({"modulenotfounderror", "importerror"}),
        "A required module could not be imported (ModuleNotFoundError): it is missing from the environment"
        " or the module path is wrong.",
        "Declare and install the missing package in the test environment, or fix the module path.",
    ),
    _ExplicitError(
        "import failure (ImportError)",
        re.compile(r"(?<!except )\bImportError(?::| while)|\bcannot import name ['\"]"),
        frozenset({"importerror", "modulenotfounderror"}),
        frozenset({"importerror"}),
        "An import failed (ImportError): a module, or a name inside it, could not be imported.",
        "Check the installed version of the module and the imported name, and look for circular imports.",
    ),
    _ExplicitError(
        "test assertion failure (AssertionError)",
        re.compile(r"\bAssertionError\b|(?:^|\s)E\s+assert\s|\bFAILED \S+::\S+ - assert\b|\bDID NOT RAISE\b"),
        frozenset({"assertion", "assertionerror"}),
        frozenset({"assertion", "assertionerror"}),
        "A test assertion failed (AssertionError): the code under test did not produce the expected result.",
        "Inspect the first failing test's assertion and the change under test, and reproduce it locally.",
    ),
)
# Pipeline stages a historical root cause can place a failure in. A root cause claiming a stage the
# log does not show (dependency installation, for a Git connection timeout) is not reused.
_STAGE_TOKENS = {
    "dependency installation": frozenset(
        {"dependency", "dependencies", "install", "installation", "requirement", "requirements"}
    ),
}
# An explicit error counts like three matching keywords toward confidence.
_EXPLICIT_ERROR_WEIGHT = 3.0
_ALL_ERROR_TOKENS = frozenset().union(*(error.incident_tokens for error in _EXPLICIT_ERRORS))
# Explicit environment errors often cause a software error downstream (a full disk fails a test,
# a registry timeout fails an install), so when one is present the explicit software error is
# treated as a symptom and the general evidence decides.
_ENVIRONMENT_ERROR = re.compile(
    r"no space left on device|out of memory|\boom(?:killed)?\b|\bMemoryError\b|timed out|\btimeout\b"
    r"|could not resolve host|name resolution|name or service not known|connection (?:refused|reset)"
    r"|\b503\b|service unavailable",
    re.IGNORECASE,
)


# Informational log levels report conditions, not failures (for example an optional import that is skipped).
_NON_FAILURE_LEVEL = re.compile(
    r"^(?:[^\t]*\t[^\t]*\t)?\ufeff?(?:\S*\d{2}:\d{2}:\d{2}\S*\s+)?(?:##)?\[?(?:WARNING|WARN|INFO|DEBUG|NOTICE)\b",
    re.IGNORECASE,
)
_TEST_FAILURE = _EXPLICIT_ERRORS[-1]


def _explicit_error(failure_text: str) -> _ExplicitError | None:
    """Return the earliest-stage explicit error actually reported, judging each log line on its own.

    A test-failure line counts only as a test failure: an exception named in its message (``expected
    ImportError``, ``DID NOT RAISE ModuleNotFoundError``) is what the test checked, not what was raised.
    """

    found: set[str] = set()
    for line in failure_text.splitlines():
        if _NON_FAILURE_LEVEL.match(line):
            continue
        if _TEST_FAILURE.pattern.search(line):
            found.add(_TEST_FAILURE.kind)
            continue
        error = next((error for error in _EXPLICIT_ERRORS if error.pattern.search(line)), None)
        if error is not None:
            found.add(error.kind)
    return next((error for error in _EXPLICIT_ERRORS if error.kind in found), None)


def _contradicts(incident: HistoricalIncident, error: _ExplicitError) -> bool:
    """Whether the incident describes a different explicit error than the one observed."""

    tokens = incident_tokens(incident)
    return bool(tokens & _ALL_ERROR_TOKENS) and not tokens & error.incident_tokens


def _environment_matches(failure: CIFailure, incident: HistoricalIncident) -> int:
    failure_environment = dict(normalize_environment(failure.environment))
    incident_environment = dict(normalize_environment(incident.environment))
    return sum(incident_environment.get(key) == value for key, value in failure_environment.items())


def _data_path() -> Path:
    return data_path("incidents.json")


def analyze_failure(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident] | None = None,
) -> AnalysisResult:
    """Analyze one failure using local keyword and environment evidence only.

    An explicit error (dependency installation failure, ModuleNotFoundError,
    ImportError, or a test assertion) identifies a software failure unless the
    log also states an explicit environment error, such as a full disk that
    made a test fail. Otherwise a category requires at least two matching
    incident keywords beyond generic CI words, or two direct category keywords.
    This favors ``unknown`` when the evidence is sparse.
    """

    available_incidents = list(incidents) if incidents is not None else load_incidents(_data_path())
    matches = find_similar_incidents(failure, available_incidents)
    failure_tokens = tokenize(failure.failure_text)
    explicit = _explicit_error(failure.failure_text)
    if explicit is not None and not _ENVIRONMENT_ERROR.search(failure.failure_text):
        return _explicit_result(failure, explicit, matches, failure_tokens)

    category_scores: dict[CauseCategory, float] = defaultdict(float)
    incident_overlap: dict[str, int] = {}
    environment_overlap: dict[str, int] = {}
    for incident in matches:
        keyword_overlap = len(failure_tokens & incident_tokens(incident))
        environment_matches = _environment_matches(failure, incident)
        incident_overlap[incident.id] = keyword_overlap
        environment_overlap[incident.id] = environment_matches
        category_scores[incident.category] += keyword_overlap + (0.5 * environment_matches)

    direct_signals = {
        category: failure_tokens & keywords
        for category, keywords in _CATEGORY_KEYWORDS.items()
        if category != "unknown"
    }
    for category, signals in direct_signals.items():
        category_scores[category] += 0.25 * len(signals)

    candidate = max(
        (category for category in _CATEGORY_KEYWORDS if category != "unknown"),
        key=lambda category: category_scores[category],
    )
    strongest_match = next((match for match in matches if match.category == candidate), None)
    strong_incident_evidence = (
        strongest_match is not None and len((failure_tokens & incident_tokens(strongest_match)) - _GENERIC_TOKENS) >= 2
    )
    strong_direct_evidence = len(direct_signals[candidate]) >= 2

    if not (strong_incident_evidence or strong_direct_evidence):
        return AnalysisResult(
            fingerprint=failure_fingerprint(failure),
            category="unknown",
            confidence=0.0,
            root_cause="Insufficient deterministic evidence to identify a root cause.",
            evidence=[],
            recommendation="Collect additional failure output and environment details, then retry analysis.",
            owner=_OWNERS["unknown"],
            matched_incident_ids=[],
        )

    evidence: list[str] = []
    for incident in matches:
        if incident.category != candidate:
            continue
        evidence.append(
            f"Historical incident {incident.id} shares {incident_overlap[incident.id]} keyword(s)"
            f" and {environment_overlap[incident.id]} environment value(s)."
        )
    if direct_signals[candidate]:
        evidence.append(f"Failure text contains {', '.join(sorted(direct_signals[candidate]))}.")

    confidence = min(0.95, 0.35 + (0.1 * category_scores[candidate]))
    if strongest_match is not None and _root_cause_applies(failure_tokens, explicit, strongest_match):
        # Packaged incidents have no root_cause, so their summary remains the output.
        root_cause = strongest_match.root_cause or strongest_match.summary
        recommendation = strongest_match.recommendation
    else:
        root_cause = f"Failure text indicates a likely {candidate} issue."
        recommendation = f"Investigate the {candidate} condition reported in the failure output."

    relevant_matches = [match.id for match in matches if match.category == candidate]
    return AnalysisResult(
        fingerprint=failure_fingerprint(failure),
        category=candidate,
        confidence=confidence,
        root_cause=root_cause,
        evidence=evidence,
        recommendation=recommendation,
        owner=_OWNERS[candidate],
        matched_incident_ids=relevant_matches,
    )


def _root_cause_applies(
    failure_tokens: set[str], explicit: _ExplicitError | None, incident: HistoricalIncident
) -> bool:
    """Whether every pipeline stage the incident's root cause claims is shown by the log's words or explicit error.

    Matching is unaffected: an incident can be a relevant match without its root cause applying.
    """

    claimed = {stage for stage, words in _STAGE_TOKENS.items() if incident_tokens(incident) & words}
    shown = {
        stage
        for stage, words in _STAGE_TOKENS.items()
        if failure_tokens & words or (explicit is not None and explicit.kind.startswith(stage))
    }
    return claimed <= shown


def _explicit_result(
    failure: CIFailure, error: _ExplicitError, matches: list[HistoricalIncident], failure_tokens: set[str]
) -> AnalysisResult:
    """Classify a failure from its explicit error, keeping only incidents consistent with it."""

    # Support: distinctive terms shared with the log, plus one when the incident names this error or a more
    # general one.
    # An incident is used only with some support, and its root cause only with at least two.
    support = {
        match.id: len((failure_tokens & incident_tokens(match)) - _GENERIC_TOKENS - _ALL_ERROR_TOKENS)
        + bool(incident_tokens(match) & error.cause_tokens)
        for match in matches
    }
    consistent = [
        match
        for match in matches
        if match.category == "software"
        and not _contradicts(match, error)
        and (incident_tokens(match) & error.incident_tokens or support[match.id] >= 1)
    ]
    overlap = {match.id: len(failure_tokens & incident_tokens(match)) for match in consistent}
    evidence = [
        f"Historical incident {match.id} shares {overlap[match.id]} keyword(s)"
        f" and {_environment_matches(failure, match)} environment value(s)."
        for match in consistent
    ]
    evidence.append(f"Failure text contains an explicit {error.kind}.")
    direct = failure_tokens & _CATEGORY_KEYWORDS["software"]
    if direct:
        evidence.append(f"Failure text contains {', '.join(sorted(direct))}.")

    # An incident naming only a more specific error (ModuleNotFoundError for an ImportError) is related,
    # but its root cause is not established by this log, however many other terms it shares.
    explaining = [
        match
        for match in consistent
        if not incident_tokens(match) & _ALL_ERROR_TOKENS or incident_tokens(match) & error.cause_tokens
    ]
    source = max(explaining, key=lambda match: support[match.id], default=None)
    if source is not None and support[source.id] >= 2:
        root_cause, recommendation = source.root_cause or source.summary, source.recommendation
    else:
        root_cause, recommendation = error.root_cause, error.recommendation
    score = _EXPLICIT_ERROR_WEIGHT + sum(overlap.values()) + 0.25 * len(direct)
    return AnalysisResult(
        fingerprint=failure_fingerprint(failure),
        category="software",
        confidence=min(0.95, 0.35 + (0.1 * score)),
        root_cause=root_cause,
        evidence=evidence,
        recommendation=recommendation,
        owner=_OWNERS["software"],
        matched_incident_ids=[match.id for match in consistent],
    )
