from pathlib import Path

import pytest

from circa.analyzer import analyze_failure
from circa.fingerprint import failure_fingerprint
from circa.knowledge import find_similar_incidents, load_incidents
from circa.knowledge_store import analysis_incidents, load_knowledge
from circa.log_parser import extract_failure_text
from circa.models import CIFailure, HistoricalIncident
from circa.pipeline import analyze_log
from circa.resources import data_path


def test_analyzer_uses_historical_match_for_network_failure() -> None:
    incidents = load_incidents(data_path("incidents.json"))
    failure = CIFailure(
        failure_text="Dependency installation hit a package registry connection timeout",
        environment={"network": "external"},
    )

    result = analyze_failure(failure, incidents)

    assert result.category == "network"
    assert result.owner == "network-ops"
    assert result.confidence > 0.5
    assert result.matched_incident_ids == ["synthetic-network-timeout-005"]
    assert result.fingerprint == failure_fingerprint(failure)
    assert result.evidence


def test_analyzer_returns_unknown_for_weak_evidence() -> None:
    result = analyze_failure(CIFailure(failure_text="Job ended unexpectedly"), [])

    assert result.category == "unknown"
    assert result.owner == "unknown"
    assert result.confidence == 0.0
    assert result.matched_incident_ids == []


# Explicit errors take precedence over keyword similarity.

ROOT = Path(__file__).parents[1]
RUN_41312342881 = Path(__file__).parent / "fixtures" / "real_incidents" / "circa-run-41312342881-pytest-assertion.log"
INCIDENTS = load_incidents(data_path("incidents.json"))


def _analyze(log: str, incidents: list[HistoricalIncident] | None = None):
    failure = CIFailure(job_name="raw-log", failure_text=extract_failure_text(log))
    return analyze_failure(failure, INCIDENTS if incidents is None else incidents)


def test_run_41312342881_pytest_assertion_is_not_a_missing_module() -> None:
    # Regression: generic words (test, process, python) made the ModuleNotFoundError incident win.
    result = _analyze(RUN_41312342881.read_text(encoding="utf-8"))

    assert result.category == "software" and result.owner == "developer"
    assert "AssertionError" in result.root_cause
    assert "ModuleNotFoundError" not in result.root_cause + result.recommendation
    assert "synthetic-software-import-004" not in result.matched_incident_ids
    assert "Failure text contains an explicit test assertion failure (AssertionError)." in result.evidence


def test_assertion_message_mentioning_a_module_is_still_an_assertion() -> None:
    result = _analyze(
        "tests/test_loader.py::test_plugins FAILED\n"
        "E       AssertionError: expected module 'circa.plugins' to be importable as a package\n"
        "tests/test_loader.py:12: AssertionError\n"
    )

    assert result.category == "software"
    assert "ModuleNotFoundError" not in result.root_cause
    assert result.matched_incident_ids == ["synthetic-software-assertion-003"]
    assert "Failure text contains an explicit test assertion failure (AssertionError)." in result.evidence


def test_pytest_assert_line_without_the_exception_name_is_an_assertion() -> None:
    result = _analyze(
        "FAILED tests/test_math.py::test_add - assert 3 == 4\n"
        "E       assert 3 == 4\n"
        "=========== 1 failed, 20 passed in 0.31s ===========\n"
    )

    assert result.category == "software" and "AssertionError" in result.root_cause


def test_module_not_found_error_is_a_missing_module() -> None:
    result = _analyze(
        "ImportError while importing test module '/build/tests/test_client.py'.\n"
        "E   ModuleNotFoundError: No module named 'requests'\n"
        "ERROR tests/test_client.py\n"
    )

    assert result.category == "software"
    assert "synthetic-software-import-004" in result.matched_incident_ids
    assert "Failure text contains an explicit missing module (ModuleNotFoundError)." in result.evidence
    assert "AssertionError" not in result.root_cause


def test_import_error_is_an_import_failure() -> None:
    result = _analyze(
        "E   ImportError: cannot import name 'parse_config' from 'circa.config' (/build/circa/config.py)\n"
    )

    assert result.category == "software"
    assert "ImportError" in result.root_cause
    assert "synthetic-software-assertion-003" not in result.matched_incident_ids


def test_dependency_installation_failure_is_a_software_dependency_failure() -> None:
    result = _analyze(
        "ERROR: Could not find a version that satisfies the requirement circa-utils==9.9.0 (from versions: none)\n"
        "ERROR: No matching distribution found for circa-utils==9.9.0\n"
        "##[error]Process completed with exit code 1.\n"
    )

    assert result.category == "software"
    assert result.root_cause.startswith("Dependency installation failed")
    assert "synthetic-network-timeout-005" not in result.matched_incident_ids


@pytest.mark.parametrize(
    "log",
    [
        "FAILED test_smoke\nError: module test failed\n##[error]Process completed with exit code 1.\n",
        "Run python -m pytest\nERROR: test process failed for module build\n",
        "Job failed. Step 'test' finished with an error.\n",
    ],
    ids=["failed-module-error", "generic-python-words", "job-step-error"],
)
def test_generic_words_alone_are_insufficient_evidence(log: str) -> None:
    result = _analyze(log)

    assert result.category == "unknown"
    assert result.confidence == 0.0 and result.matched_incident_ids == []


def test_explicit_assertion_wins_over_weak_keywords_of_another_error() -> None:
    # The log shares dependency, package, and import words with the missing-module incident,
    # but the failure itself is an assertion.
    result = _analyze(
        "E       AssertionError: dependency package list changed after import of the test python process\n"
        "tests/test_lock.py:30: AssertionError\n"
    )

    assert result.category == "software" and "AssertionError" in result.root_cause
    assert "synthetic-software-import-004" not in result.matched_incident_ids


def test_earlier_pipeline_errors_outrank_a_later_assertion() -> None:
    result = _analyze(
        "E   ModuleNotFoundError: No module named 'yaml'\nE       AssertionError: assert loader is not None\n"
    )

    assert "Failure text contains an explicit missing module (ModuleNotFoundError)." in result.evidence


def test_strong_environment_evidence_is_not_overridden_by_an_assertion() -> None:
    # A full disk made the test fail; the assertion is a symptom.
    result = _analyze(
        "OSError: [Errno 28] No space left on device: '/build/out/report.bin'\n"
        "worker disk full while writing compiler output\n"
        "E       AssertionError: report was not written\n"
    )

    assert result.category == "hardware"
    assert "synthetic-hardware-disk-001" in result.matched_incident_ids


def test_verified_team_incident_with_observed_evidence_still_supplies_the_root_cause() -> None:
    team = HistoricalIncident.model_validate(
        {
            "id": "team-requests-missing-001",
            "title": "requests missing from the integration test image",
            "category": "software",
            "summary": "ModuleNotFoundError for requests in the integration image.",
            "keywords": ["modulenotfounderror", "requests", "integration", "image"],
            "recommendation": "Add requests to the integration image requirements.",
            "root_cause": "The integration image was rebuilt without requests.",
            "evidence": ["E   ModuleNotFoundError: No module named 'requests'"],
            "status": "verified",
            "synthetic": True,
        }
    )

    result = _analyze("E   ModuleNotFoundError: No module named 'requests'\n", [*INCIDENTS, team])

    assert result.root_cause == "The integration image was rebuilt without requests."
    assert result.matched_incident_ids[0] == "team-requests-missing-001"


def test_explicit_results_keep_the_fingerprint_and_retrieval_unchanged() -> None:
    log = RUN_41312342881.read_text(encoding="utf-8")
    failure = CIFailure(job_name="raw-log", failure_text=extract_failure_text(log))

    assert analyze_failure(failure, INCIDENTS).fingerprint == failure_fingerprint(failure)
    # Retrieval ranking itself is not changed; the analyzer only filters what contradicts the evidence.
    assert find_similar_incidents(failure, INCIDENTS)[0].id == "synthetic-software-import-004"


def test_install_failure_caused_by_a_registry_timeout_stays_a_network_failure() -> None:
    result = _analyze(
        "WARNING: Retrying after connection broken by 'ReadTimeoutError(\"HTTPSConnectionPool(host='pypi.org',"
        " port=443): Read timed out. (read timeout=15)\")': /simple/requests/\n"
        "ERROR: Could not find a version that satisfies the requirement requests (from versions: none)\n"
        "ERROR: No matching distribution found for requests\n"
        "Dependency installation hit a package registry connection timeout\n"
    )

    assert result.category == "network"
    assert "synthetic-network-timeout-005" in result.matched_incident_ids


def test_pytest_collection_import_error_is_an_import_failure() -> None:
    result = _analyze("ImportError while importing test module '/build/tests/test_config.py'.\n")

    assert result.category == "software" and "ImportError" in result.root_cause


def test_direct_category_keywords_without_incidents_give_a_generic_result() -> None:
    result = analyze_failure(CIFailure(failure_text="dns lookup could not resolve the hostname"), [])

    assert result.category == "network"
    assert result.root_cause == "Failure text indicates a likely network issue."
    assert result.matched_incident_ids == []


# Phase 14 review: precedence between explicit errors, and matching on short error signatures.


def _explicit_kind(result) -> str:
    return next(line for line in result.evidence if "explicit" in line)


@pytest.mark.parametrize(
    "log",
    [
        (
            "E       AssertionError: expected ImportError when the optional plugin is missing\n"
            "tests/test_plugins.py:41: AssertionError\n"
        ),
        (
            "E       Failed: DID NOT RAISE <class 'ModuleNotFoundError'>\n"
            "FAILED tests/test_loader.py::test_missing - Failed: DID NOT RAISE <class 'ModuleNotFoundError'>\n"
        ),
        (
            "2026-11-01T10:00:00Z WARNING: speedups disabled (ModuleNotFoundError: No module named 'ujson')\n"
            "E       AssertionError: assert 3 == 4\n"
        ),
        (
            "    try:\n        import ujson\n>   except ImportError:\n"
            "        with pytest.raises(ModuleNotFoundError):\nE       AssertionError: assert None\n"
        ),
    ],
    ids=["error-named-in-assertion", "did-not-raise", "warning-line", "traceback-source-lines"],
)
def test_errors_that_were_not_raised_do_not_outrank_the_test_failure(log: str) -> None:
    result = _analyze(log)

    assert _explicit_kind(result) == "Failure text contains an explicit test assertion failure (AssertionError)."
    assert "ModuleNotFoundError" not in result.root_cause and "synthetic-software-import-004" not in (
        result.matched_incident_ids
    )


def test_a_test_failing_with_an_import_error_is_an_import_failure() -> None:
    result = _analyze("FAILED tests/test_cfg.py::test_load - ImportError: cannot import name 'load' from 'cfg'\n")

    assert _explicit_kind(result) == "Failure text contains an explicit import failure (ImportError)."


def _verified(incident_id: str, keywords: list[str], root_cause: str, evidence: list[str]) -> HistoricalIncident:
    """A verified team incident; its ``evidence`` must occur in the analyzed log for it to take part."""

    return HistoricalIncident.model_validate(
        {
            "id": incident_id,
            "title": incident_id,
            "category": "software",
            "summary": root_cause,
            "keywords": keywords,
            "recommendation": "Apply the documented fix.",
            "root_cause": root_cause,
            "evidence": evidence,
            "status": "verified",
            "synthetic": True,
        }
    )


def test_an_incident_that_only_mentions_import_does_not_contradict_an_assertion() -> None:
    line = "E       AssertionError: golden snapshot mismatch in report.json"
    golden = _verified(
        "team-golden-001", ["golden", "snapshot", "import"], "Import order changed the golden file.", [line]
    )

    result = _analyze(line + "\n", [*INCIDENTS, golden])

    assert result.matched_incident_ids == ["team-golden-001"]
    assert result.root_cause == "Import order changed the golden file."


def test_an_import_error_incident_explains_a_missing_module_with_one_shared_term() -> None:
    line = "E   ModuleNotFoundError: No module named 'ujson'"
    ujson = _verified("team-ujson-001", ["importerror", "ujson", "arm64"], "ujson had no arm64 wheel.", [line])

    result = _analyze(line + "\n", [*INCIDENTS, ujson])

    assert result.matched_incident_ids[0] == "team-ujson-001"
    assert result.root_cause == "ujson had no arm64 wheel."


def test_a_missing_module_incident_does_not_explain_a_cannot_import_name_error() -> None:
    result = _analyze("E   ImportError: cannot import name 'parse' from 'circa.config'\n")

    assert "ModuleNotFoundError" not in result.root_cause
    assert result.root_cause.startswith("An import failed (ImportError)")


def test_a_missing_module_incident_never_explains_an_import_error_even_with_shared_terms() -> None:
    # "cannot import name ... from 'yaml'" means yaml is installed; a "yaml not installed" incident is related, not the cause.
    line = "E   ImportError: cannot import name 'SafeLoader' from 'yaml' (/venv/yaml/__init__.py)"
    yaml_missing = _verified(
        "team-yaml-missing-001", ["modulenotfounderror", "yaml", "loader"], "pyyaml was not installed.", [line]
    )

    result = _analyze(line + "\nE   loader setup failed\n", [*INCIDENTS, yaml_missing])

    assert "team-yaml-missing-001" in result.matched_incident_ids
    assert result.root_cause.startswith("An import failed (ImportError)")


# Phase 16A: diff content, root-cause eligibility, and npm failures.


def test_source_printed_in_a_ruff_diff_is_not_a_raised_error() -> None:
    log = "\n".join(
        "Lint (Ruff)\tRuff format check\t2026-10-23T16:42:10.9330518Z " + line
        for line in [
            "--- tests/test_models.py",
            "+++ tests/test_models.py",
            "@@ -2,9 +2,7 @@",
            '-        {"failure_text": "AssertionError: expected 2"}',
            '+    failure = CIFailure.model_validate({"failure_text": "AssertionError: expected 2"})',
            "12 files would be reformatted, 22 files already formatted",
            "##[error]Process completed with exit code 1.",
        ]
    )

    result = _analyze(log)

    # The summary is a formatting failure (Phase 16B); the AssertionError inside the diff is not raised.
    assert _explicit_kind(result) == "Failure text contains an explicit formatting check failure."
    assert "synthetic-software-assertion-003" not in result.matched_incident_ids


GIT_TIMEOUT = (
    "fatal: unable to access 'https://github.com/example/lib.git/': Failed to connect to github.com port 443"
    " after 134912 ms: Connection timed out\n##[error]Process completed with exit code 128.\n"
)


def test_a_dependency_installation_root_cause_is_not_reused_for_a_git_timeout() -> None:
    result = _analyze(GIT_TIMEOUT)

    assert result.category == "network"
    assert "synthetic-network-timeout-005" in result.matched_incident_ids  # still a relevant match
    assert result.root_cause == "Failure text indicates a likely network issue."
    assert "Dependency" not in result.recommendation


def test_a_dependency_installation_root_cause_applies_when_the_log_shows_that_stage() -> None:
    timeout_root_cause = next(item for item in INCIDENTS if item.id == "synthetic-network-timeout-005").summary
    pip_timeout = (
        "WARNING: Retrying after connection broken by 'ReadTimeoutError(\"HTTPSConnectionPool(host='pypi.org',"
        " port=443): Read timed out. (read timeout=15)\")': /simple/requests/\n"
        "ERROR: Could not find a version that satisfies the requirement requests (from versions: none)\n"
    )

    structured = CIFailure(failure_text="Dependency download timed out while contacting the registry.")
    assert analyze_failure(structured, INCIDENTS).root_cause == timeout_root_cause
    # The explicit dependency-installation error shows the stage even without the word "dependency".
    assert _analyze(pip_timeout).root_cause == timeout_root_cause


@pytest.mark.parametrize(
    ("log", "incident_id"),
    [
        ("[  412.118] Out of memory: Killed process 8123 (python)\n", "synthetic-hardware-memory-002"),
        (
            "Provisioning failed: container runner cannot locate its configured image version.\n",
            "synthetic-infrastructure-image-007",
        ),
    ],
    ids=["oom", "image"],
)
def test_root_causes_that_claim_no_pipeline_stage_are_still_reused(log: str, incident_id: str) -> None:
    incident = next(item for item in INCIDENTS if item.id == incident_id)

    assert _analyze(log).root_cause == incident.summary


def test_npm_dependency_resolution_failure_is_a_software_dependency_failure() -> None:
    log = (
        "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree\n"
        'npm ERR! Could not resolve dependency:\nnpm ERR! peer react@"^17.0.0" from legacy-widget@2.4.0\n'
    )

    result = _analyze(log)

    assert result.category == "software"
    assert result.root_cause.startswith("Dependency installation failed")


def test_an_unresolvable_host_still_marks_an_environment_error() -> None:
    log = "curl: (6) Could not resolve host: files.internal.example\nE       AssertionError: download missing\n"

    assert not any("explicit" in line for line in _analyze(log).evidence)


# Phase 16B: quality-gate failures (lint, formatting, type checking, security, compiler, syntax).


@pytest.mark.parametrize(
    ("log", "kind", "detail"),
    [
        (
            (
                "src/net/socket.c:42:5: error: implicit declaration of function 'set_nonblocking'\n"
                "make: *** [Makefile:12: all] Error 2\n"
            ),
            "compiler error",
            "src/net/socket.c:42:5: error: implicit declaration of function 'set_nonblocking'",
        ),
        (
            "main.cpp:3:10: fatal error: missing.h: No such file or directory\ncompilation terminated.\n",
            "compiler error",
            "main.cpp:3:10: fatal error: missing.h",
        ),
        (
            (
                'Traceback (most recent call last):\n  File "/build/run.py", line 2, in <module>\n'
                "  File \"/build/tools/gen_config.py\", line 18\n    if mode = 'release':\n"
                "SyntaxError: invalid syntax\n"
            ),
            "syntax error (SyntaxError)",
            'File "/build/tools/gen_config.py", line 18; SyntaxError: invalid syntax',
        ),
        ("E     IndentationError: unexpected indent\n", "syntax error (SyntaxError)", "IndentationError"),
        (
            (
                "src/circa/cli.py:167: error: Incompatible types in assignment  [assignment]\n"
                "Found 1 error in 1 file (checked 17 source files)\n"
            ),
            "type-check failure (mypy)",
            "src/circa/cli.py:167: error: Incompatible types in assignment",
        ),
        (
            (
                "src/app.py:1:1: I001 [*] Import block is un-sorted or un-formatted\nFound 1 error.\n"
                "[*] 1 fixable with the `--fix` option.\n"
            ),
            "lint failure",
            "src/app.py:1:1: I001 [*] Import block is un-sorted",
        ),
        ("I001 [*] Import block is un-sorted or un-formatted\nFound 3 errors.\n", "lint failure", ""),
        (
            "Would reformat: src/app.py\n2 files would be reformatted, 30 files already formatted\n",
            "formatting check failure",
            "2 files would be reformatted",
        ),
        (
            "[warn] docs/guide.md\n[warn] Code style issues found in the above file. Run Prettier with --write to fix.\n",
            "formatting check failure",
            "Code style issues found in the above file",
        ),
        (
            (
                ">> Issue: [B310:blacklist] Audit url open for permitted schemes.\n   Severity: Medium   Confidence: High\n"
                "   Location: src/circa/llm.py:46:17\n"
            ),
            "security scan finding (Bandit)",
            "[B310:blacklist] Audit url open for permitted schemes.; Location: src/circa/llm.py:46:17",
        ),
    ],
    ids=[
        "gcc",
        "missing-header",
        "syntax",
        "indentation",
        "mypy",
        "ruff-lint",
        "ruff-lint-new-format",
        "ruff-format",
        "prettier",
        "bandit",
    ],
)
def test_quality_gate_failures_are_recognized_with_their_details(log: str, kind: str, detail: str) -> None:
    result = _analyze(log)

    assert result.category == "software" and result.owner == "developer"
    assert _explicit_kind(result) == f"Failure text contains an explicit {kind}."
    assert detail in result.root_cause


@pytest.mark.parametrize(
    "log",
    [
        "Run ruff check .\nRun mypy\nRun bandit -r src\nRun prettier --check .\n##[error]Process completed with exit code 1.\n",
        (
            "Success: no issues found in 21 source files\nAll checks passed!\n41 files already formatted\n"
            "All matched files use Prettier code style!\nNo issues identified.\n##[error]Process completed with exit code 1.\n"
        ),
        "Found 0 errors.\n0 files would be reformatted\n##[error]Process completed with exit code 1.\n",
        "/build/x.py:3: SyntaxWarning: invalid escape sequence '\\d'\n##[error]Process completed with exit code 1.\n",
        "    except SyntaxError:\n##[error]Process completed with exit code 1.\n",
        "error: mypy: can't read file 'missing.py': No such file or directory\n",
    ],
    ids=["tool-names", "success-summaries", "zero-counts", "syntax-warning", "except-clause", "mypy-usage-error"],
)
def test_tool_names_success_output_and_warnings_are_not_quality_gate_failures(log: str) -> None:
    result = _analyze(log)

    assert result.category == "unknown"
    assert not any("explicit" in line for line in result.evidence)


def test_a_compiler_killed_for_memory_is_not_a_compiler_error() -> None:
    result = _analyze("c++: fatal error: Killed signal terminated program cc1plus\ncompilation terminated.\n")

    assert not any("explicit" in line for line in result.evidence)


def test_runtime_failures_outrank_static_checks_in_the_same_log() -> None:
    log = (
        "src/a.py:3: error: Incompatible return value type  [return-value]\n"
        "Found 1 error in 1 file (checked 4 source files)\n"
        "E   ModuleNotFoundError: No module named 'yaml'\n"
    )

    assert _explicit_kind(_analyze(log)) == "Failure text contains an explicit missing module (ModuleNotFoundError)."


def test_an_environment_error_still_takes_precedence_over_a_quality_gate_summary() -> None:
    log = (
        "OSError: [Errno 28] No space left on device\nworker disk full while writing compiler output\nFound 2 errors.\n"
    )

    assert _analyze(log).category == "hardware"


def test_bandit_code_snippets_are_not_failure_evidence() -> None:
    gh = "Security (Bandit)\tRun Bandit\t2026-10-23T16:42:13.8758687Z "
    log = "\n".join(
        gh + line
        for line in [
            ">> Issue: [B310:blacklist] Audit url open for permitted schemes.",
            "   Severity: Medium   Confidence: High",
            "   Location: src/circa/llm.py:46:17",
            "45\t        try:",
            "46\t            with urlopen(request, timeout=self.timeout) as response:",
            "",
            "##[error]Process completed with exit code 1.",
        ]
    )

    result = _analyze(log)

    # "timeout" in the printed source must not trigger the network fallback.
    assert result.category == "software"
    assert _explicit_kind(result) == "Failure text contains an explicit security scan finding (Bandit)."


def test_a_verified_incident_naming_the_tool_supplies_the_root_cause() -> None:
    team = _verified(
        "team-ruff-format-001",
        ["ruff", "reformatted", "quality", "gate"],
        "The quality gate was enabled before the code was formatted.",
        ["12 files would be reformatted, 22 files already formatted", "quality gate failed"],
    )

    result = _analyze(
        "12 files would be reformatted, 22 files already formatted\nquality gate failed\n", [*INCIDENTS, team]
    )

    assert result.root_cause == "The quality gate was enabled before the code was formatted."


# Phase 22: a team incident informs a failure only when its recorded evidence, including a distinctive
# line, is observed in the log. Shared words and tool summaries are similarity, not evidence.

PRETTIER_SUMMARY = (
    "Format (Prettier)\tCheck formatting\t2026-11-01T17:31:41.0204453Z [warn] Code style issues found in 2 files. "
    "Run Prettier with --write to fix."
)
NOTES_LINE = "Format (Prettier)\tCheck formatting\t2026-11-01T17:31:40.8905916Z [warn] notes.md"
TEAM_ROOT_CAUSE = "Release-notes scratch files were committed by mistake."


def _team(evidence: list[str], category: str = "software", keywords: tuple[str, ...] = ()) -> HistoricalIncident:
    return HistoricalIncident.model_validate(
        {
            "id": "team-scratch-files-001",
            "title": "Prettier failed on release-notes scratch files committed to the repository",
            "category": category,
            "summary": "Code style issues found in scratch release notes files found in the repository.",
            "keywords": ["prettier", "code style issues found", "release notes", *keywords],
            "recommendation": "Remove the scratch files.",
            "root_cause": TEAM_ROOT_CAUSE,
            "evidence": evidence,
            "status": "verified",
            "synthetic": True,
        }
    )


def test_shared_generic_words_do_not_carry_an_unrelated_incidents_category_or_root_cause() -> None:
    incident = _team([NOTES_LINE, PRETTIER_SUMMARY], category="hardware", keywords=("repository", "release", "rule"))
    log = "HttpError: Repository rule violations found for release v1.2\nCannot update this protected ref.\n"

    with_team = analyze_log(log, incidents=[*INCIDENTS, incident])

    # Evidence is insufficient, so the result is the same safe unknown as without team knowledge.
    assert with_team == analyze_log(log, incidents=INCIDENTS)
    assert with_team.category == "unknown" and with_team.matched_incident_ids == []
    assert TEAM_ROOT_CAUSE not in with_team.root_cause


def test_the_tt_metal_protected_tag_failure_inherits_nothing_from_circas_team_incidents() -> None:
    # Phase 21: "found", "release", and "repository" made this GitHub ruleset rejection "software", with
    # CIRCA's own scratch-files commit as its root cause.
    log = (ROOT / "evaluation" / "logs" / "tt-metal-protected-tag.log").read_text(encoding="utf-8")
    team = analysis_incidents(load_knowledge(ROOT / "team" / "incidents.json").incidents)
    team_root_causes = {incident.root_cause for incident in team if incident.root_cause}

    result = analyze_log(log, incidents=team)

    assert result.category == "unknown" and result.matched_incident_ids == []
    assert result.root_cause not in team_root_causes


def test_an_incident_whose_distinctive_evidence_is_observed_still_explains_the_failure() -> None:
    later = [line.replace("2026-11-01T17:31", "2026-11-09T08:02") for line in (NOTES_LINE, PRETTIER_SUMMARY)]
    log = "\n".join([*later, "Format (Prettier)\tCheck formatting\t2026-11-09T08:02:41Z ##[error]Process completed"])

    result = analyze_log(log, incidents=[*INCIDENTS, _team([NOTES_LINE, PRETTIER_SUMMARY])])

    assert result.category == "software"
    assert result.matched_incident_ids == ["team-scratch-files-001"]
    assert result.root_cause == TEAM_ROOT_CAUSE


def test_observed_tool_summaries_alone_do_not_ground_an_incident() -> None:
    # Every two-file Prettier failure prints this line; the incident's own file name is not in this log.
    result = analyze_log(PRETTIER_SUMMARY + "\n", incidents=[*INCIDENTS, _team([PRETTIER_SUMMARY])])

    # Deterministic classification is unaffected: the formatter rule still explains the failure.
    assert result.category == "software" and result.matched_incident_ids == []
    assert result.root_cause.startswith("A formatting check failed")


def test_evidence_is_grounded_in_the_whole_log_not_only_the_extracted_failure_text() -> None:
    # The file-name line is context the failure-text extractor can leave out; grounding reads the full log.
    filler = [f"Format (Prettier)\tCheck formatting\t2026-11-09T08:02:3{n}Z Checking formatting..." for n in range(5)]
    log = "\n".join([NOTES_LINE, *filler, PRETTIER_SUMMARY])

    result = analyze_log(log, incidents=[*INCIDENTS, _team([NOTES_LINE, PRETTIER_SUMMARY])])

    assert result.matched_incident_ids == ["team-scratch-files-001"]
