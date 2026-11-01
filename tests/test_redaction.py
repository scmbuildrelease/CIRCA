import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from circa import analyzer, pipeline
from circa.cli import app
from circa.models import CIFailure, LLMDiagnosis
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
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJ0ZXN0In0" + ".c2lnbmF0dXJlLXRlc3Q"
PASSWORD = "Synth3tic" + "-Passw0rd"
KEY_BODY = "MIIEow" + "IBAAKCAQEAsynthetic0key0material0only"
BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
END = "-----END RSA " + "PRIVATE KEY-----"
ALL = [GITHUB, GITHUB_FINE, GITLAB, AWS_ID, SLACK, NPM, JWT, PASSWORD, KEY_BODY]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"push rejected for {GITHUB}", "push rejected for [REDACTED]"),
        (f"token {GITHUB_FINE} expired", "token [REDACTED] expired"),
        (f"clone failed using {GITLAB}", "clone failed using [REDACTED]"),
        (f"InvalidClientTokenId: {AWS_ID} is not valid", "InvalidClientTokenId: [REDACTED] is not valid"),
        (f"post failed: {SLACK}", "post failed: [REDACTED]"),
        (f"npm ERR! 401 with {NPM}", "npm ERR! 401 with [REDACTED]"),
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
