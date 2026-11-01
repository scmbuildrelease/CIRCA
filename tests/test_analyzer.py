from pathlib import Path

import pytest

from circa.analyzer import analyze_failure
from circa.fingerprint import failure_fingerprint
from circa.knowledge import find_similar_incidents, load_incidents
from circa.log_parser import extract_failure_text
from circa.models import CIFailure, HistoricalIncident
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


def test_verified_team_incident_with_shared_terms_still_supplies_the_root_cause() -> None:
    team = HistoricalIncident.model_validate(
        {
            "id": "team-requests-missing-001",
            "title": "requests missing from the integration test image",
            "category": "software",
            "summary": "ModuleNotFoundError for requests in the integration image.",
            "keywords": ["modulenotfounderror", "requests", "integration", "image"],
            "recommendation": "Add requests to the integration image requirements.",
            "root_cause": "The integration image was rebuilt without requests.",
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


def _verified(incident_id: str, keywords: list[str], root_cause: str) -> HistoricalIncident:
    return HistoricalIncident.model_validate(
        {
            "id": incident_id,
            "title": incident_id,
            "category": "software",
            "summary": root_cause,
            "keywords": keywords,
            "recommendation": "Apply the documented fix.",
            "root_cause": root_cause,
            "status": "verified",
            "synthetic": True,
        }
    )


def test_an_incident_that_only_mentions_import_does_not_contradict_an_assertion() -> None:
    golden = _verified("team-golden-001", ["golden", "snapshot", "import"], "Import order changed the golden file.")

    result = _analyze("E       AssertionError: golden snapshot mismatch in report.json\n", [*INCIDENTS, golden])

    assert result.matched_incident_ids == ["team-golden-001"]
    assert result.root_cause == "Import order changed the golden file."


def test_an_import_error_incident_explains_a_missing_module_with_one_shared_term() -> None:
    ujson = _verified("team-ujson-001", ["importerror", "ujson", "arm64"], "ujson had no arm64 wheel.")

    result = _analyze("E   ModuleNotFoundError: No module named 'ujson'\n", [*INCIDENTS, ujson])

    assert result.matched_incident_ids[0] == "team-ujson-001"
    assert result.root_cause == "ujson had no arm64 wheel."


def test_a_missing_module_incident_does_not_explain_a_cannot_import_name_error() -> None:
    result = _analyze("E   ImportError: cannot import name 'parse' from 'circa.config'\n")

    assert "ModuleNotFoundError" not in result.root_cause
    assert result.root_cause.startswith("An import failed (ImportError)")


def test_a_missing_module_incident_never_explains_an_import_error_even_with_shared_terms() -> None:
    # "cannot import name ... from 'yaml'" means yaml is installed; a "yaml not installed" incident is related, not the cause.
    yaml_missing = _verified(
        "team-yaml-missing-001", ["modulenotfounderror", "yaml", "loader"], "pyyaml was not installed."
    )

    result = _analyze(
        "E   ImportError: cannot import name 'SafeLoader' from 'yaml' (/venv/yaml/__init__.py)\nE   loader setup failed\n",
        [*INCIDENTS, yaml_missing],
    )

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

    assert not any("explicit" in line for line in result.evidence)
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
