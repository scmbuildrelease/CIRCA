import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from circa import analyzer, pipeline
from circa.cli import app
from circa.knowledge_store import KnowledgeError, load_knowledge
from circa.models import CIFailure, HistoricalIncident, LLMDiagnosis
from circa.pipeline import analyze_log, investigate_log
from circa.redaction import RedactionError, contains_secret, redact_secrets

runner = CliRunner()

# Synthetic credentials, assembled at runtime so no secret-shaped literal is committed
# for the repository's Gitleaks scan to report. None of them is a real credential.
GITHUB = "ghp" + "_" + "A1b2C3d4" * 5
GITHUB_FINE = "github" + "_pat_" + "11ABCDEFG0" * 3
GITLAB = "glpat" + "-" + "x7Y8z9W0" * 3
AWS_ID = "AKIA" + "SYNTHETIC0EXAMPL"
SLACK = "xoxb" + "-1234567890-abcdefghij"
NPM = "npm" + "_" + "q7W8e9R0" * 4 + "abcd"
PYPI = "pypi" + "-" + "AgEIcHlwaS5vcmc" + "x" * 40
GOOGLE = "AIza" + "Sy" + "Synthetic0Key0Material0Only001234"
OPENAI = "sk" + "-proj-" + "Synthetic0Key0Material0Only"
ANTHROPIC = "sk" + "-ant-" + "api03-Synthetic0Key0Material0Only"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJ0ZXN0In0" + ".c2lnbmF0dXJlLXRlc3Q"
PASSWORD = "Synth3tic" + "-Passw0rd"
KEY_BODY = "MIIEow" + "IBAAKCAQEAsynthetic0key0material0only"
BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
END = "-----END RSA " + "PRIVATE KEY-----"
ALL = [GITHUB, GITHUB_FINE, GITLAB, AWS_ID, SLACK, NPM, PYPI, GOOGLE, OPENAI, ANTHROPIC, JWT, PASSWORD, KEY_BODY]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"push rejected for {GITHUB}", "push rejected for [REDACTED]"),
        (f"token {GITHUB_FINE} expired", "token [REDACTED] expired"),
        (f"clone failed using {GITLAB}", "clone failed using [REDACTED]"),
        (f"InvalidClientTokenId: {AWS_ID} is not valid", "InvalidClientTokenId: [REDACTED] is not valid"),
        (f"post failed: {SLACK}", "post failed: [REDACTED]"),
        (f"npm ERR! 401 with {NPM}", "npm ERR! 401 with [REDACTED]"),
        (f"twine upload failed for {PYPI}", "twine upload failed for [REDACTED]"),
        (f"Maps request rejected for {GOOGLE}", "Maps request rejected for [REDACTED]"),
        (
            f"openai.AuthenticationError: Incorrect API key {OPENAI}",
            "openai.AuthenticationError: Incorrect API key [REDACTED]",
        ),
        (f"anthropic 401 invalid x-api-key {ANTHROPIC}", "anthropic 401 invalid x-api-key [REDACTED]"),
        (f"jwt {JWT} rejected", "jwt [REDACTED] rejected"),
        (f"Authorization: Bearer {JWT}", "Authorization: Bearer [REDACTED]"),
        ("Authorization: token abcd1234efgh", "Authorization: token [REDACTED]"),
        (f'curl -H "Authorization: Basic {PASSWORD}" failed', 'curl -H "Authorization: Basic [REDACTED]" failed'),
        ("401 for bearer abc123def456ghi", "401 for bearer [REDACTED]"),
        (f"password={PASSWORD}", "password=[REDACTED]"),
        (f"DB_PASSWORD: {PASSWORD} rejected", "DB_PASSWORD: [REDACTED] rejected"),
        (f"export AWS_SECRET_ACCESS_KEY={PASSWORD}", "export AWS_SECRET_ACCESS_KEY=[REDACTED]"),
        (f'{{"api_key": "{PASSWORD}"}}', '{"api_key": "[REDACTED]"}'),
        (f"login --password {PASSWORD} failed", "login --password [REDACTED] failed"),
        (
            f"fatal: unable to access 'https://ci-bot:{PASSWORD}@git.example.com/x.git/'",
            "fatal: unable to access 'https://ci-bot:[REDACTED]@git.example.com/x.git/'",
        ),
        (f"postgres://app:{PASSWORD}@db:5432/main refused", "postgres://app:[REDACTED]@db:5432/main refused"),
    ],
    ids=[
        "github",
        "github-fine-grained",
        "gitlab",
        "aws-key-id",
        "slack",
        "npm",
        "pypi",
        "google",
        "openai",
        "anthropic",
        "jwt",
        "authorization-bearer",
        "authorization-token",
        "authorization-basic-quoted",
        "bare-bearer",
        "password",
        "env-password",
        "env-aws-secret",
        "json-api-key",
        "password-option",
        "url-https",
        "url-database",
    ],
)
def test_secret_values_are_redacted_and_the_message_is_kept(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected
    assert contains_secret(text) and not contains_secret(expected)


@pytest.mark.parametrize(
    "text",
    [
        "ERROR: connection timed out after 30s while fetching https://pypi.org/simple/",
        "token expired while fetching artifacts",
        "Error: Resource not accessible by integration; check the token permissions",
        "the password was rejected by the server",
        "src/auth.py:12: error: Incompatible types in assignment (expression has type int)",
        "def login(user: str, password: str) -> None:",
        "max_tokens=4096 exceeded the model limit",
        "Authorization failed: 403 Forbidden",
        "Bearer authentication is not supported",
        "connecting to http://localhost:8080/health",
        "git@github.com:scmbuildrelease/CIRCA.git: Permission denied (publickey)",
        "headers, AWS tokens, `password=`-style assignments",
        "sha256:5c5d50febdac1526d42f86da6672f04c4f041032ff52ed4c4fb8d999e98ba027",
        "AUTHORIZATION: basic ***",
        "NPM_TOKEN=*** was masked",
        "password=<redacted>",
        "GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}",
        "export API_KEY=$API_KEY",
    ],
)
def test_ordinary_masked_and_referenced_text_is_unchanged(text: str) -> None:
    assert redact_secrets(text) == text
    assert not contains_secret(text)


def test_multiple_secrets_on_one_line_and_across_lines_are_all_redacted() -> None:
    log = f"login {GITHUB} and {SLACK} with password={PASSWORD}\nkey {AWS_ID}\nok\n"

    redacted = redact_secrets(log)

    assert redacted == "login [REDACTED] and [REDACTED] with password=[REDACTED]\nkey [REDACTED]\nok\n"
    assert redact_secrets(redacted) == redacted


def test_private_key_is_redacted_from_begin_through_end() -> None:
    log = f"Writing key\n{BEGIN}\n{KEY_BODY}\n{KEY_BODY[:20]}==\n{END}\nerror: deploy failed\n"

    assert redact_secrets(log) == (
        "Writing key\n[REDACTED]\n[REDACTED]\n[REDACTED]\n[REDACTED]\nerror: deploy failed\n"
    )


def test_unterminated_and_prefixed_private_keys_stop_at_the_first_non_key_line() -> None:
    prefix = "build\tdeploy\t2026-10-24T09:30:07.1234567Z "
    log = f"{prefix}{BEGIN}\n{prefix}{KEY_BODY}\n{prefix}error: key could not be loaded\n"

    redacted = redact_secrets(log)

    assert KEY_BODY not in redacted and "error: key could not be loaded" in redacted


def test_a_private_key_on_one_line_or_json_escaped_is_redacted_without_touching_other_lines() -> None:
    document = json.dumps({"evidence": f"{BEGIN}\n{KEY_BODY}\n{END}", "next": "line"}, indent=2)

    redacted = redact_secrets(document)

    assert json.loads(redacted) == {"evidence": "[REDACTED]", "next": "line"}


def test_cifailure_input_is_redacted_on_construction() -> None:
    failure = CIFailure(failure_text=f"auth failed with {GITHUB}", environment={"token": f"TOKEN={PASSWORD}"})

    assert failure.failure_text == "auth failed with [REDACTED]"
    assert failure.environment == {"token": "TOKEN=[REDACTED]"}


SECRET_LOG = (
    "Run ./deploy.sh\n"
    f"error: Authorization: Bearer {JWT} rejected by registry.example.com\n"
    f"ERROR: npm publish failed with NPM_TOKEN={NPM}\n"
    f"fatal: unable to access 'https://ci-bot:{PASSWORD}@git.example.com/x.git/': 403\n"
    f"ValueError: bad credentials {AWS_ID} and {GITHUB}\n"
    f"{BEGIN}\n{KEY_BODY}\n{END}\n"
    "##[error]Process completed with exit code 1.\n"
)


def _leaks(text: str) -> list[str]:
    return [secret for secret in ALL if secret in text]


def test_cli_text_and_json_output_never_contain_secrets(tmp_path: Path) -> None:
    log = tmp_path / "secret.log"
    log.write_text(SECRET_LOG, encoding="utf-8")

    outputs = [
        runner.invoke(app, ["analyze-log", str(log)]),
        runner.invoke(app, ["analyze-log", str(log), "--json"]),
        runner.invoke(app, ["investigate-log", str(log), "--json"]),
        runner.invoke(app, ["investigate-log", str(log)]),
    ]

    assert all(result.exit_code == 0 for result in outputs), [result.output for result in outputs]
    assert all(_leaks(result.output) == [] for result in outputs)
    evidence = json.loads(outputs[2].output)["evidence"]
    assert "ERROR: npm publish failed with NPM_TOKEN=[REDACTED]" in evidence  # the message stays readable


class _EchoingProvider:
    """A local model stand-in that repeats the evidence it was given in its diagnosis."""

    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def diagnose(self, prompt: str) -> LLMDiagnosis:
        self.prompts.append(prompt)
        quoted = "ERROR: npm publish failed with NPM_TOKEN=[REDACTED]"
        return LLMDiagnosis(
            category="software",
            confidence=0.7,
            root_cause="npm publish failed because the NPM_TOKEN credential was rejected.",
            evidence=[quoted],
            suggested_fix="Rotate the npm publish credential.",
        )


def test_prompt_and_saved_draft_hold_only_redacted_evidence(tmp_path: Path) -> None:
    provider = _EchoingProvider()

    investigation = investigate_log(SECRET_LOG, [], provider, source_name="secret.log")

    assert investigation.draft is not None
    assert _leaks(provider.prompts[0]) == [] and "NPM_TOKEN=[REDACTED]" in provider.prompts[0]
    assert _leaks(json.dumps(investigation.draft)) == []
    assert "ERROR: npm publish failed with NPM_TOKEN=[REDACTED]" in investigation.draft["evidence"]


def test_invalid_analyze_input_is_reported_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "failure.json"
    path.write_text(json.dumps({"failure_text": 1, "environment": {"token": GITHUB}, "x": PASSWORD}), encoding="utf-8")

    result = runner.invoke(app, ["analyze", str(path)])

    assert result.exit_code == 1
    assert "invalid CIFailure JSON: failure_text" in result.output
    assert _leaks(result.output) == []


@pytest.mark.parametrize("text", [SECRET_LOG, f"password={PASSWORD} and {GITHUB}", f"{BEGIN}\n{KEY_BODY}\n"])
def test_redaction_is_idempotent(text: str) -> None:
    once = redact_secrets(text)

    assert redact_secrets(once) == once
    assert not contains_secret(once)


def test_redaction_happens_before_every_downstream_reader() -> None:
    readers = ["extract_failure_text", "failure_evidence_lines", "analyze_performance_failure", "dedup_fingerprint"]
    spies = {name: MagicMock(wraps=getattr(pipeline, name)) for name in readers}
    provider = _EchoingProvider()

    with (
        patch.multiple(pipeline, **spies),
        patch.object(analyzer, "failure_fingerprint", wraps=analyzer.failure_fingerprint) as fingerprint,
    ):
        investigate_log(SECRET_LOG, [], provider, source_name="secret.log")

    received = [repr(call) for spy in [*spies.values(), fingerprint] for call in spy.call_args_list]
    assert all(spy.called for spy in spies.values()) and fingerprint.called
    assert _leaks("".join(received)) == [] and _leaks("".join(provider.prompts)) == []


def _swap_secrets(log: str) -> str:
    replacements = {
        GITHUB: "ghp" + "_" + "Z9y8X7w6" * 5,
        NPM: "npm" + "_" + "a1S2d3F4" * 4 + "wxyz",
        PASSWORD: "Other0" + "-Value9",
    }
    for old, new in replacements.items():
        log = log.replace(old, new)
    return log


def test_the_same_failure_with_different_secrets_has_the_same_fingerprints() -> None:
    other = _swap_secrets(SECRET_LOG)
    assert other != SECRET_LOG

    first, second = (investigate_log(log, [], None, source_name="x") for log in (SECRET_LOG, other))

    assert first.dedup_fingerprint == second.dedup_fingerprint
    assert first.analysis.fingerprint == second.analysis.fingerprint
    assert analyze_log(SECRET_LOG).fingerprint == analyze_log(other).fingerprint


@pytest.mark.parametrize(
    ("name", "dedup", "analysis"),
    [
        (
            "demo_disk_full.log",
            "12d10f2fa9713579f92be955d470a7e6e6085ec956881dec976b842719e08433",
            "8d765a3ffda765b075f77658080166de73d6c627774e276bf5d8ae38b6a0bacd",
        ),
        (
            "demo_unknown_failure.log",
            "06a3cf72d814a881ebc7411ba226b947f917fca205ee39d47e98bf84b968ae50",
            "607ec94a11e4d83a5d2268064b9105f0b070a7c31d1c68d93a3fa71b1351c0e2",
        ),
    ],
)
def test_secret_free_fingerprints_are_unchanged(name: str, dedup: str, analysis: str) -> None:
    # Values recorded before redaction was introduced.
    log = (Path(__file__).parents[1] / "examples" / name).read_text(encoding="utf-8")

    assert redact_secrets(log) == log
    assert investigate_log(log, [], None, source_name=name).dedup_fingerprint == dedup
    assert analyze_log(log).fingerprint == analysis


class _BrokenPattern:
    def sub(self, *_: object) -> str:
        raise RuntimeError(f"engine failure while reading {PASSWORD}")


def test_failed_redaction_raises_an_error_that_carries_no_text() -> None:
    with patch("circa.redaction._SECRET", _BrokenPattern()), pytest.raises(RedactionError) as raised:
        redact_secrets(SECRET_LOG)

    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    assert _leaks(str(error)) == [] and "unredacted" in str(error)


def test_failed_redaction_stops_the_pipeline_before_any_downstream_reader() -> None:
    readers = ["extract_failure_text", "failure_evidence_lines", "analyze_performance_failure", "dedup_fingerprint"]
    spies = {name: MagicMock(wraps=getattr(pipeline, name)) for name in readers}
    provider = _EchoingProvider()

    with patch("circa.redaction._SECRET", _BrokenPattern()), patch.multiple(pipeline, **spies):
        with pytest.raises(RedactionError):
            investigate_log(SECRET_LOG, [], provider, source_name="secret.log")
        with pytest.raises(RedactionError):
            analyze_log(SECRET_LOG)
        with pytest.raises(RedactionError):
            CIFailure(failure_text=SECRET_LOG)

    assert not any(spy.called for spy in spies.values()) and provider.prompts == []


def test_failed_redaction_fails_every_cli_path_closed(tmp_path: Path) -> None:
    log = tmp_path / "secret.log"
    log.write_text(SECRET_LOG, encoding="utf-8")
    failure = tmp_path / "failure.json"
    failure.write_text(json.dumps({"failure_text": SECRET_LOG}), encoding="utf-8")
    knowledge = tmp_path / "team.json"
    provider = _EchoingProvider()

    with patch("circa.redaction._SECRET", _BrokenPattern()), patch("circa.cli.OllamaProvider", return_value=provider):
        results = [
            runner.invoke(app, ["analyze-log", str(log)]),
            runner.invoke(app, ["analyze-log", str(log), "--json"]),
            runner.invoke(app, ["analyze", str(failure), "--json"]),
            runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge), "--json"]),
        ]

    for result in results:
        assert result.exit_code == 1, result.output
        assert "credential redaction failed" in result.output and _leaks(result.output) == []
        assert not isinstance(result.exception, RedactionError)  # handled, not a traceback
    assert provider.prompts == [] and not knowledge.exists()


def test_multi_megabyte_logs_are_redacted_in_one_fast_pass() -> None:
    block = (
        "2026-10-24T09:30:07Z [INFO] compiling module app.core with flags -O2 -Wall\n"
        "2026-10-24T09:30:08Z [INFO] fetched https://registry.example.com/simple/pkg/ in 31 ms\n"
    ) * 50
    log = (block + f"ERROR: upload failed with NPM_TOKEN={NPM}\n") * 600  # about 4.9 MB
    assert len(log) > 4_500_000

    started = time.perf_counter()
    redacted = redact_secrets(log)
    elapsed = time.perf_counter() - started

    assert _leaks(redacted) == [] and redacted.count("NPM_TOKEN=[REDACTED]") == 600
    assert redacted.count("\n") == log.count("\n")
    assert elapsed < 10  # about 0.6 s locally; the bound only catches pathological slowdowns


# Phase 17.1: draft provenance and LLM output.

EXAMPLES = Path(__file__).parents[1] / "examples"
UNKNOWN_LOG = (EXAMPLES / "demo_unknown_failure.log").read_text(encoding="utf-8")
SIGNTOOL_LINE = "2026-10-24T09:30:07Z [ERROR] signtool rejected bundle: certificate chain expired on 2026-10-23"


def _draft_with_cited_line(line: str, identifier: str = "team-sign-001") -> dict[str, object]:
    return {
        "id": identifier,
        "title": "Signing certificate chain expired",
        "category": "infrastructure",
        "summary": "signtool rejected the bundle because the certificate chain expired.",
        "recommendation": "Renew the signing certificate chain.",
        "synthetic": False,
        "evidence": [SIGNTOOL_LINE],
        "draft_origin": {"source_log": "build.log", "confidence": 0.6, "cited_log_lines": [line]},
    }


@pytest.mark.parametrize(
    "secret",
    [GITHUB, f"password={PASSWORD}", f"https://ci-bot:{PASSWORD}@git.example.com"],
    ids=["github", "password", "url"],
)
def test_a_secret_in_cited_log_lines_is_rejected(secret: str) -> None:
    record = _draft_with_cited_line(f"upload failed with {secret}") | {"status": "pending_review"}

    with pytest.raises(ValueError, match="secret or credential pattern"):
        HistoricalIncident.model_validate(record)
    assert HistoricalIncident.model_validate(_draft_with_cited_line(SIGNTOOL_LINE) | {"status": "pending_review"})


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_knowledge_add_and_update_reject_a_secret_in_cited_log_lines(tmp_path: Path) -> None:
    knowledge = tmp_path / "team.json"
    unsafe = _write_json(tmp_path / "unsafe.json", _draft_with_cited_line(f"auth failed: {GITHUB}"))

    added = runner.invoke(app, ["knowledge", "add", str(unsafe), "--knowledge", str(knowledge), "--yes"])
    assert added.exit_code == 1 and "secret or credential pattern" in added.output
    assert not knowledge.exists() and _leaks(added.output) == []

    safe = _write_json(tmp_path / "safe.json", _draft_with_cited_line(SIGNTOOL_LINE))
    assert runner.invoke(app, ["knowledge", "add", str(safe), "--knowledge", str(knowledge), "--yes"]).exit_code == 0
    before = knowledge.read_bytes()

    updated = runner.invoke(app, ["knowledge", "update", str(unsafe), "--knowledge", str(knowledge), "--yes"])
    assert updated.exit_code == 1 and "secret or credential pattern" in updated.output
    assert knowledge.read_bytes() == before and _leaks(updated.output) == []


def test_loading_a_knowledge_file_with_a_secret_in_cited_log_lines_fails(tmp_path: Path) -> None:
    record = _draft_with_cited_line(f"auth failed: {GITHUB}") | {"status": "pending_review"}
    knowledge = _write_json(tmp_path / "team.json", [record])

    with pytest.raises(KnowledgeError, match="secret or credential pattern") as raised:
        load_knowledge(knowledge)
    validated = runner.invoke(app, ["knowledge", "validate", str(knowledge)])

    assert _leaks(str(raised.value)) == []
    assert validated.exit_code == 1 and _leaks(validated.output) == []


class _LeakyProvider:
    """A local model stand-in that invents credentials in every generated field."""

    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def diagnose(self, prompt: str) -> LLMDiagnosis:
        self.prompts.append(prompt)
        return LLMDiagnosis(
            category="infrastructure",
            confidence=0.6,
            root_cause=f"signtool certificate chain expired; the job used {GITHUB}.",
            evidence=[SIGNTOOL_LINE, f"auth {OPENAI}"],
            suggested_fix=f"Renew the signtool certificate with password={PASSWORD}.",
            validation_steps=[f"Re-run signtool with {ANTHROPIC}."],
            missing_evidence=[f"Whether {AWS_ID} is still active."],
        )


def test_generated_credentials_never_leave_investigate_log() -> None:
    investigation = investigate_log(UNKNOWN_LOG, [], _LeakyProvider(), source_name="build.log")

    assert _leaks(investigation.model_dump_json()) == []
    assert investigation.outcome == "deterministic_only" and investigation.draft is None
    assert any("the LLM output contained a credential" in note for note in investigation.notes)
    assert investigation.diagnosis is not None
    assert investigation.diagnosis.root_cause == "signtool certificate chain expired; the job used [REDACTED]."
    assert investigation.diagnosis.evidence == [SIGNTOOL_LINE]  # grounding still applies after redaction


def test_secret_free_generated_text_still_produces_a_draft() -> None:
    class _CleanProvider(_LeakyProvider):
        def diagnose(self, prompt: str) -> LLMDiagnosis:
            return LLMDiagnosis(
                category="infrastructure",
                confidence=0.6,
                root_cause="signtool rejected the bundle because the certificate chain expired.",
                evidence=[SIGNTOOL_LINE],
                suggested_fix="Renew the signtool certificate chain.",
            )

    investigation = investigate_log(UNKNOWN_LOG, [], _CleanProvider(), source_name="build.log")

    assert investigation.outcome == "new_diagnosis" and investigation.draft is not None


def test_generated_credentials_never_reach_cli_output_or_knowledge(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    log.write_text(UNKNOWN_LOG, encoding="utf-8")
    knowledge = tmp_path / "team.json"

    with patch("circa.cli.OllamaProvider", return_value=_LeakyProvider()):
        text = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge)])
        as_json = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge), "--json"])

    assert text.exit_code == 0 and as_json.exit_code == 0, text.output + as_json.output
    assert _leaks(text.output) == [] and _leaks(as_json.output) == []
    assert json.loads(as_json.output)["outcome"] == "deterministic_only"
    assert not knowledge.exists()


def test_failed_redaction_of_generated_text_fails_closed(tmp_path: Path) -> None:
    calls = {"count": 0}

    def redact_then_fail(text: str) -> str:
        calls["count"] += 1
        if calls["count"] > 1:  # the log is redacted; the model's output is not
            raise RedactionError("credential redaction failed, so CIRCA stopped without using the unredacted text")
        return redact_secrets(text)

    with patch.object(pipeline, "redact_secrets", side_effect=redact_then_fail):
        with pytest.raises(RedactionError):
            investigate_log(UNKNOWN_LOG, [], _LeakyProvider(), source_name="build.log")
        log = tmp_path / "build.log"
        log.write_text(UNKNOWN_LOG, encoding="utf-8")
        calls["count"] = 0
        knowledge = tmp_path / "team.json"
        with patch("circa.cli.OllamaProvider", return_value=_LeakyProvider()):
            result = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge), "--json"])

    assert result.exit_code == 1 and "credential redaction failed" in result.output
    assert _leaks(result.output) == [] and not knowledge.exists()


# Phase 18: legacy knowledge files and caller-supplied draft provenance.


def test_a_legacy_record_with_a_secret_names_the_fields_but_never_the_value(tmp_path: Path) -> None:
    record = _draft_with_cited_line(SIGNTOOL_LINE) | {
        "status": "pending_review",
        "evidence": [SIGNTOOL_LINE, f"ERROR: publish failed NPM_TOKEN={NPM}"],
        "environment": {"github_auth": GITHUB},
    }
    knowledge = _write_json(tmp_path / "team.json", [record])

    with pytest.raises(KnowledgeError) as raised:
        load_knowledge(knowledge)
    outputs = [
        runner.invoke(app, ["knowledge", "validate", str(knowledge)]),
        runner.invoke(app, ["analyze-log", str(EXAMPLES / "demo_disk_full.log"), "--knowledge", str(knowledge)]),
        runner.invoke(app, ["doctor", "--knowledge", str(knowledge)]),
    ]

    message = str(raised.value)
    assert "record 0" in message and "in evidence[1], environment;" in message
    assert _leaks(message) == []
    for result in outputs:
        assert result.exit_code == 1 and "evidence[1], environment" in result.output and _leaks(result.output) == []


def test_replacing_the_credential_with_the_placeholder_repairs_a_legacy_record(tmp_path: Path) -> None:
    record = _draft_with_cited_line(SIGNTOOL_LINE) | {"status": "pending_review"}
    unsafe = record | {"evidence": [SIGNTOOL_LINE, f"ERROR: publish failed NPM_TOKEN={NPM}"]}
    knowledge = _write_json(tmp_path / "team.json", [unsafe])
    assert runner.invoke(app, ["knowledge", "validate", str(knowledge)]).exit_code == 1

    knowledge.write_text(knowledge.read_text(encoding="utf-8").replace(NPM, "[REDACTED]"), encoding="utf-8")

    assert runner.invoke(app, ["knowledge", "validate", str(knowledge)]).exit_code == 0
    assert load_knowledge(knowledge).incidents[0].evidence[1] == "ERROR: publish failed NPM_TOKEN=[REDACTED]"


class _NamedProvider(_LeakyProvider):
    model = "sk" + "-proj-" + "Synthetic0Model0Setting0Only"

    def diagnose(self, prompt: str) -> LLMDiagnosis:
        return LLMDiagnosis(
            category="infrastructure",
            confidence=0.6,
            root_cause="signtool rejected the bundle because the certificate chain expired.",
            evidence=[SIGNTOOL_LINE],
            suggested_fix="Renew the signtool certificate chain.",
        )


def test_credentials_in_the_source_name_and_model_are_redacted_in_the_draft(tmp_path: Path) -> None:
    investigation = investigate_log(UNKNOWN_LOG, [], _NamedProvider(), source_name=f"ci-{GITHUB}.log")

    assert investigation.draft is not None
    origin = investigation.draft["draft_origin"]
    assert origin["source_log"] == "ci-[REDACTED].log" and origin["llm_model"] == "[REDACTED]"
    assert "Synthetic0Model" not in investigation.model_dump_json() and _leaks(investigation.model_dump_json()) == []

    log = tmp_path / f"ci-{GITHUB}.log"
    log.write_text(UNKNOWN_LOG, encoding="utf-8")
    knowledge = tmp_path / "team.json"
    with patch("circa.cli.OllamaProvider", return_value=_NamedProvider()):
        result = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge)])

    assert result.exit_code == 0 and "Draft saved" in result.output and _leaks(result.output) == []
    assert _leaks(knowledge.read_text(encoding="utf-8")) == [] and "Synthetic0Model" not in knowledge.read_text()


# v0.2.1: URL passwords that contain "@". The authority's last "@" separates the
# user information from the host, so the whole password must be redacted.

AT_PASSWORD = "S3cretP" + "@ssw0rdX"
MULTI_AT_PASSWORD = "Mu1tiQ" + "@AtVal" + "@Pass9Z"


def _password_fragments(text: str, password: str) -> int:
    """Count the password and each of its "@"-separated pieces in ``text``, without echoing them."""

    return sum(piece in text for piece in [password, *password.split("@")])


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("https://deploy:{pw}@host/", "https://deploy:[REDACTED]@host/"),
        ("https://deploy:{pw}@host:8443/repo.git", "https://deploy:[REDACTED]@host:8443/repo.git"),
        (
            "https://deploy:{pw}@host.example/p/a?ref=main&x=a@b#frag",
            "https://deploy:[REDACTED]@host.example/p/a?ref=main&x=a@b#frag",
        ),
        ("https://deploy:{pw}@host?next=a@b", "https://deploy:[REDACTED]@host?next=a@b"),
        ("postgres://app:{pw}@db:5432/main refused", "postgres://app:[REDACTED]@db:5432/main refused"),
        (
            "fatal: unable to access 'https://ci-bot:{pw}@git.example.com/x.git/': 403",
            "fatal: unable to access 'https://ci-bot:[REDACTED]@git.example.com/x.git/': 403",
        ),
        (
            "see https://ci:{pw}@h.example and mail ops@example.com",
            "see https://ci:[REDACTED]@h.example and mail ops@example.com",
        ),
    ],
    ids=["path", "port-path", "query-fragment", "at-in-query-without-path", "database", "git-error", "later-email"],
)
@pytest.mark.parametrize("password", [AT_PASSWORD, MULTI_AT_PASSWORD, PASSWORD], ids=["one-at", "multiple-at", "plain"])
def test_url_passwords_are_fully_redacted(template: str, expected: str, password: str) -> None:
    redacted = redact_secrets(template.format(pw=password))

    assert redacted == expected
    assert _password_fragments(redacted, password) == 0, "part of the URL password remains"
    assert redact_secrets(redacted) == redacted


@pytest.mark.parametrize(
    "url",
    [
        "https://host.example/path?q=1#f",
        "https://user@host.example/repo.git",
        "ssh://git@github.com/scmbuildrelease/CIRCA.git",
        "https://host.example:8443/a@b",
        "https://deploy:[REDACTED]@host/",
        "https://deploy:***@host/",
        "https://deploy:$CI_PASSWORD@host/",
    ],
    ids=["no-credentials", "user-only", "ssh-user", "at-in-path", "already-redacted", "masked", "variable"],
)
def test_urls_without_a_password_value_are_unchanged(url: str) -> None:
    assert redact_secrets(url) == url
    assert not contains_secret(url)


def test_an_at_sign_url_password_never_crosses_a_security_boundary(tmp_path: Path) -> None:
    url_line = f"fatal: unable to access 'https://ci-bot:{MULTI_AT_PASSWORD}@git.example.com/x.git/': 403"
    log = tmp_path / "clone.log"
    log.write_text(UNKNOWN_LOG + f"2026-10-24T09:30:08Z [ERROR] {url_line}\n", encoding="utf-8")
    knowledge = tmp_path / "team.json"

    class _UrlEchoingProvider(_LeakyProvider):
        def diagnose(self, prompt: str) -> LLMDiagnosis:
            self.prompts.append(prompt)
            return LLMDiagnosis(
                category="infrastructure",
                confidence=0.6,
                root_cause=f"signtool certificate chain expired; the clone used {url_line}",
                evidence=[SIGNTOOL_LINE],
            )

    provider = _UrlEchoingProvider()
    with patch("circa.cli.OllamaProvider", return_value=provider):
        investigated = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge), "--json"])
    analyzed = runner.invoke(app, ["analyze-log", str(log), "--json"])
    direct = investigate_log(log.read_text(encoding="utf-8"), [], _UrlEchoingProvider(), source_name="clone.log")

    assert investigated.exit_code == 0 and analyzed.exit_code == 0
    checked = [investigated.output, analyzed.output, *provider.prompts, direct.model_dump_json()]
    assert all(_password_fragments(text, MULTI_AT_PASSWORD) == 0 for text in checked), "URL password crossed a boundary"
    redacted_evidence = (
        "https://ci-bot:[REDACTED]@git.example.com/x.git/" in json.loads(investigated.output)["evidence"][-1]
    )
    assert redacted_evidence, "the evidence line is missing its redacted URL"
    assert not knowledge.exists()  # the generated credential blocked the draft

    with pytest.raises(ValueError, match="secret or credential pattern"):
        HistoricalIncident.model_validate(_draft_with_cited_line(url_line) | {"status": "pending_review"})


def test_a_saved_draft_citing_an_at_sign_url_keeps_no_part_of_the_password(tmp_path: Path) -> None:
    url_line = f"2026-10-24T09:30:08Z [ERROR] fatal: unable to access 'https://ci-bot:{MULTI_AT_PASSWORD}@git.example.com/x.git/': 403"
    log = tmp_path / "clone.log"
    log.write_text(UNKNOWN_LOG + url_line + "\n", encoding="utf-8")
    knowledge = tmp_path / "team.json"

    class _CitingProvider(_LeakyProvider):
        def diagnose(self, prompt: str) -> LLMDiagnosis:
            cited = [line.strip("- ").strip() for line in prompt.splitlines() if "git.example.com" in line]
            return LLMDiagnosis(
                category="infrastructure",
                confidence=0.6,
                root_cause="signtool rejected the bundle because the certificate chain expired.",
                evidence=[SIGNTOOL_LINE, *cited[:1]],
                suggested_fix="Renew the signtool certificate chain.",
            )

    with patch("circa.cli.OllamaProvider", return_value=_CitingProvider()):
        result = runner.invoke(app, ["investigate-log", str(log), "--knowledge", str(knowledge), "--json"])

    saved = knowledge.read_text(encoding="utf-8")
    assert result.exit_code == 0
    redacted_url = "https://ci-bot:[REDACTED]@git.example.com/x.git/" in saved
    assert redacted_url, "the saved draft is missing its redacted URL"
    assert _password_fragments(saved, MULTI_AT_PASSWORD) == 0, "URL password reached the knowledge file"
    assert [record.status for record in load_knowledge(knowledge).incidents] == ["pending_review"]
