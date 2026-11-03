"""Deterministic local CI failure analysis."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .fingerprint import (
    evidence_observed,
    failure_fingerprint,
    normalize_environment,
    normalize_volatile_text,
    normalized_log_lines,
)
from .knowledge import find_similar_incidents, incident_tokens, load_incidents, tokenize
from .log_parser import record_text
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
    # Where and what was reported (file, line, message), appended to the root cause when found.
    details: tuple[re.Pattern[str], ...] = ()
    # Some tools print their failing summary at an informational level (Prettier uses "[warn]").
    any_level: bool = False


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
        "compiler error",
        re.compile(r"\S+\.(?:c|cc|cpp|cxx|h|hh|hpp|m|mm):\d+:\d+: (?:fatal )?error: "),
        frozenset({"gcc", "clang"}),
        frozenset({"gcc", "clang"}),
        "Compilation failed: the compiler reported an error in the source.",
        "Fix the first reported compiler error; later errors are often consequences of it.",
        (re.compile(r"\S+\.(?:c|cc|cpp|cxx|h|hh|hpp|m|mm):\d+:\d+: (?:fatal )?error: [^\n]*"),),
    ),
    _ExplicitError(
        "syntax error (SyntaxError)",
        re.compile(r"(?<!except )\b(?:SyntaxError|IndentationError|TabError): "),
        frozenset({"syntaxerror", "indentationerror", "taberror"}),
        frozenset({"syntaxerror", "indentationerror", "taberror"}),
        "The code could not be parsed (SyntaxError).",
        "Fix the syntax at the reported file and line, and run the code or its tests locally.",
        # The last traceback frame is the file that failed to parse.
        (
            re.compile(r'File "[^"\n]+", line \d+(?![\s\S]*File ")'),
            re.compile(r"\b(?:SyntaxError|IndentationError|TabError): [^\n]*"),
        ),
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
    # Static checks: summaries and error signatures that only these tools print, never their names alone.
    _ExplicitError(
        "type-check failure (mypy)",
        re.compile(
            r"\bFound [1-9]\d* errors? in \d+ files? \(checked \d+ source files?\)"
            r"|\S+\.pyi?:\d+: error: .*\[[a-z][a-z-]*\]\s*$"
        ),
        frozenset({"mypy"}),
        frozenset({"mypy"}),
        "Type checking failed (mypy): the code has type errors.",
        "Fix the reported type errors and rerun mypy locally.",
        (re.compile(r"\S+\.pyi?:\d+: error: [^\n]*"),),
    ),
    _ExplicitError(
        "lint failure",
        re.compile(r"\bFound [1-9]\d* errors?\.(?:\s|$)|\[\*\] \d+ fixable with\b"),
        frozenset({"ruff"}),
        frozenset({"ruff"}),
        "The linter reported rule violations.",
        "Fix the reported violations, or apply the linter's safe fixes, and rerun it locally.",
        (re.compile(r"\S+\.\w+:\d+:\d+: [A-Z]+\d+ [^\n]*"),),
    ),
    _ExplicitError(
        "formatting check failure",
        re.compile(r"\b[1-9]\d* files? would be reformatted\b|\b[Ww]ould reformat:? \S|\bCode style issues found in\b"),
        frozenset({"prettier", "ruff"}),
        frozenset({"prettier", "ruff"}),
        "A formatting check failed: files are not formatted as the project's formatter requires.",
        "Run the project's formatter (for example `ruff format` or `prettier --write`) and commit the result.",
        (re.compile(r"\b[1-9]\d* files? would be reformatted[^\n]*|\bCode style issues found in[^\n]*"),),
        any_level=True,
    ),
    _ExplicitError(
        "security scan finding (Bandit)",
        re.compile(r">> Issue: \[B\d{3}:\w+\]"),
        frozenset({"bandit"}),
        frozenset({"bandit"}),
        "The security scanner (Bandit) reported findings that fail the build.",
        "Review each reported issue: fix it, or add a narrow, justified suppression.",
        (re.compile(r"\[B\d{3}:\w+\][^\n]*"), re.compile(r"Location: \S+:\d+:\d+")),
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
_TEST_FAILURE = next(error for error in _EXPLICIT_ERRORS if error.kind.startswith("test assertion"))
_DETAIL_LIMIT = 160


def _explicit_error(failure_text: str) -> _ExplicitError | None:
    """Return the earliest-stage explicit error actually reported, judging each log line on its own.

    A test-failure line counts only as a test failure: an exception named in its message (``expected
    ImportError``, ``DID NOT RAISE ModuleNotFoundError``) is what the test checked, not what was raised.
    """

    found: set[str] = set()
    for line in failure_text.splitlines():
        if _NON_FAILURE_LEVEL.match(line):
            found.update(error.kind for error in _EXPLICIT_ERRORS if error.any_level and error.pattern.search(line))
            continue
        if _TEST_FAILURE.pattern.search(line):
            found.add(_TEST_FAILURE.kind)
            continue
        error = next((error for error in _EXPLICIT_ERRORS if error.pattern.search(line)), None)
        if error is not None:
            found.add(error.kind)
    return next((error for error in _EXPLICIT_ERRORS if error.kind in found), None)


def _reported_details(failure_text: str, error: _ExplicitError) -> str:
    """The first file, line, and message the tool reported, as one clipped string ("" if none)."""

    found = (pattern.search(failure_text) for pattern in error.details)
    return "; ".join(match.group(0).strip()[:_DETAIL_LIMIT] for match in found if match)


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


# A specific subject: a file or path name, a version, a dotted name, or a quoted identifier.
_SPECIFIC_SUBJECT = re.compile(r"\w[./]\w|['\"`][\w.-]+['\"`]")
# Placeholders that normalize_volatile_text puts where timestamps, hashes, and other volatile values were.
_PLACEHOLDER_TOKENS = frozenset({"timestamp", "time", "uuid", "address", "hash", "temp-path", "duration", "n", "id"})


def _distinctive_evidence(line: str) -> bool:
    """Whether a recorded evidence line can tell its incident apart from other failures of the same kind.

    A tool's failure summary ("Code style issues found in 2 files", "Found 9 errors in 6 files") and the
    runner's exit status appear in every failure of that kind, so they count only when they name a specific
    file, version, or identifier. Any other line counts when it has words beyond generic CI terms.
    """

    text = normalize_volatile_text(record_text(line))
    if _SPECIFIC_SUBJECT.search(text):
        return True
    if any(error.pattern.search(record_text(line)) for error in _EXPLICIT_ERRORS):
        return False
    terms = {term.strip("./#[]-") for term in tokenize(text)} - _GENERIC_TOKENS - _PLACEHOLDER_TOKENS
    return any(term and not term.isdigit() for term in terms)


def _grounded(incident: HistoricalIncident, log_lines: list[str]) -> bool:
    """Whether an incident may inform this failure's category, root cause, and recommendation.

    An incident that records evidence or a root cause (team knowledge) must have all its evidence observed in
    the log, including at least one distinctive line; shared words alone are only similarity. Packaged
    synthetic incidents record neither: they are keyword signatures, matched by their keywords as before.
    """

    if not incident.evidence and not incident.root_cause:
        return True
    return evidence_observed(incident.evidence, log_lines) and any(map(_distinctive_evidence, incident.evidence))


def _signature_observed(incident: HistoricalIncident, failure_tokens: set[str]) -> bool:
    """Whether the failure shows what identifies the incident, not just words from its description.

    A grounded incident with recorded evidence was already observed in the log. Otherwise at least one of
    its declared keywords (every word of a phrase such as "no space") must appear: sharing only ordinary
    words of its title or summary ("build", "no", "left") does not make a full disk.
    """

    if incident.evidence:
        return True
    # tokenize keeps dots for versions and paths, so a sentence-final "full." must still match "full".
    observed = failure_tokens | {token.rstrip(".") for token in failure_tokens}
    return any((words := tokenize(keyword)) and words <= observed for keyword in incident.keywords)


def analyze_failure(
    failure: CIFailure,
    incidents: Iterable[HistoricalIncident] | None = None,
    *,
    log: str | None = None,
) -> AnalysisResult:
    """Analyze one failure using local keyword and environment evidence only.

    An explicit error (dependency installation failure, ModuleNotFoundError,
    ImportError, or a test assertion) identifies a software failure unless the
    log also states an explicit environment error, such as a full disk that
    made a test fail. Otherwise a category requires at least two matching
    incident keywords beyond generic CI words, or two direct category keywords.
    This favors ``unknown`` when the evidence is sparse.

    A historical incident with recorded evidence takes part only if that evidence is grounded in ``log``
    (the whole redacted log; ``failure.failure_text`` when not given).
    """

    available_incidents = list(incidents) if incidents is not None else load_incidents(_data_path())
    log_lines = normalized_log_lines(failure.failure_text if log is None else log)
    available_incidents = [incident for incident in available_incidents if _grounded(incident, log_lines)]
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
    strongest_match = next(
        (match for match in matches if match.category == candidate and _signature_observed(match, failure_tokens)),
        None,
    )
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
    details = _reported_details(failure.failure_text, error)
    if details:
        evidence.append(f"Reported: {details}")
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
        if details:
            root_cause = f"{root_cause} First reported: {details}"
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
