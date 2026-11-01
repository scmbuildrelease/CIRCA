import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from circa.cli import app, safe_text
from circa.doctor import Check, knowledge_check, run_checks
from circa.github_runs import GitHubRunError, detect_repository, github_repository_from_remote
from circa.llm import OllamaProvider

runner = CliRunner()
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


# Repository detection


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/scmbuildrelease/CIRCA.git",
        "https://github.com/scmbuildrelease/CIRCA",
        "https://github.com/scmbuildrelease/CIRCA/",
        "git@github.com:scmbuildrelease/CIRCA.git",
        "git@github.com:scmbuildrelease/CIRCA",
        "ssh://git@github.com/scmbuildrelease/CIRCA.git",
        "ssh://git@github.com:22/scmbuildrelease/CIRCA.git",
        "https://user:secret@github.com/scmbuildrelease/CIRCA.git",
    ],
    ids=["https-git", "https", "https-slash", "scp-ssh", "scp-ssh-no-git", "ssh-url", "ssh-url-port", "userinfo"],
)
def test_github_remote_formats_are_recognized(url: str) -> None:
    assert github_repository_from_remote(url) == "scmbuildrelease/CIRCA"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/scmbuildrelease/CIRCA.git",
        "git@bitbucket.org:scmbuildrelease/CIRCA.git",
        "https://github.example.com/scmbuildrelease/CIRCA.git",
        "https://github.com/scmbuildrelease/..",
        "git@github.com:scmbuildrelease/CIRCA/extra.git",
        "/srv/git/CIRCA.git",
    ],
    ids=["gitlab", "bitbucket", "enterprise-host", "invalid-name", "extra-path", "local-path"],
)
def test_unsupported_hosts_and_invalid_names_are_not_github_repositories(url: str) -> None:
    assert github_repository_from_remote(url) is None


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run([GIT, "-C", str(directory), *arguments], check=True, capture_output=True)


@pytest.fixture
def isolated_git(monkeypatch, tmp_path: Path) -> Path:
    # A user's global config (for example url.insteadOf rewrites) must not affect the result.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "checkout"
    repository.mkdir()
    return repository


@needs_git
def test_single_github_remote_is_detected(isolated_git: Path) -> None:
    _git(isolated_git, "init", "-q")
    _git(isolated_git, "remote", "add", "origin", "git@github.com:scmbuildrelease/CIRCA.git")

    assert detect_repository(isolated_git) == ("scmbuildrelease/CIRCA", ["origin"])


@needs_git
def test_remotes_naming_the_same_repository_are_not_ambiguous(isolated_git: Path) -> None:
    _git(isolated_git, "init", "-q")
    _git(isolated_git, "remote", "add", "origin", "https://github.com/scmbuildrelease/CIRCA.git")
    _git(isolated_git, "remote", "add", "mirror", "git@github.com:SCMBuildRelease/circa.git")
    _git(isolated_git, "remote", "add", "internal", "https://gitlab.example.com/team/circa.git")

    repository, remotes = detect_repository(isolated_git)

    assert repository.lower() == "scmbuildrelease/circa"
    assert remotes == ["mirror", "origin"]


@needs_git
def test_remotes_naming_different_repositories_are_never_chosen_silently(isolated_git: Path) -> None:
    _git(isolated_git, "init", "-q")
    _git(isolated_git, "remote", "add", "origin", "git@github.com:someone/CIRCA.git")
    _git(isolated_git, "remote", "add", "upstream", "https://github.com/scmbuildrelease/CIRCA.git")

    with pytest.raises(GitHubRunError) as error:
        detect_repository(isolated_git)

    message = str(error.value)
    assert "different GitHub repositories" in message
    assert "origin -> someone/CIRCA" in message and "upstream -> scmbuildrelease/CIRCA" in message
    assert "pass OWNER/REPO" in message


@needs_git
def test_unsupported_host_is_reported_without_the_remote_url(isolated_git: Path) -> None:
    _git(isolated_git, "init", "-q")
    _git(isolated_git, "remote", "add", "origin", "https://deploy:glpat-secret-token@gitlab.com/team/circa.git")

    with pytest.raises(GitHubRunError) as error:
        detect_repository(isolated_git)

    assert "no Git remote points to github.com (remotes: origin (gitlab.com))" in str(error.value)
    assert "glpat-secret-token" not in str(error.value) and "deploy" not in str(error.value)


@needs_git
def test_repository_without_remotes_or_outside_git_is_reported(isolated_git: Path, tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(GitHubRunError, match="not inside a Git repository"):
        detect_repository(outside)
    _git(isolated_git, "init", "-q")
    with pytest.raises(GitHubRunError, match="has no remotes"):
        detect_repository(isolated_git)


def test_missing_git_is_reported() -> None:
    with patch("circa.github_runs.shutil.which", return_value=None), pytest.raises(GitHubRunError, match="Git was not"):
        detect_repository()


def test_git_timeout_is_reported() -> None:
    with (
        patch("circa.github_runs.shutil.which", return_value="/usr/bin/git"),
        patch("circa.github_runs.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)) as run,
        pytest.raises(GitHubRunError, match="could not run Git"),
    ):
        detect_repository()
    assert run.call_args.kwargs["shell"] is False


def _detected(monkeypatch, repository: str = "scmbuildrelease/CIRCA") -> None:
    monkeypatch.setattr("circa.cli.detect_repository", lambda: (repository, ["origin"]))


def _gh_run(stdout: str):
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    return (
        patch("circa.github_runs.shutil.which", return_value="/usr/bin/gh"),
        patch("circa.github_runs.subprocess.run", return_value=completed),
    )


RUN_LIST = json.dumps(
    [
        {
            "databaseId": 39988949831,
            "workflowName": "CI",
            "headBranch": "main",
            "conclusion": "failure",
            "createdAt": "2026-10-23T16:42:04Z",
            "url": "https://github.com/scmbuildrelease/CIRCA/actions/runs/39988949831",
            "displayTitle": "t",
        }
    ]
)


def test_runs_without_a_repository_uses_the_detected_one(monkeypatch) -> None:
    _detected(monkeypatch)
    which, run = _gh_run(RUN_LIST)

    with which, run as gh:
        result = runner.invoke(app, ["runs", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["run_id"] == "39988949831"
    assert gh.call_args.args[0][3:5] == ["--repo", "scmbuildrelease/CIRCA"]
    assert "Using scmbuildrelease/CIRCA (from git remote origin)" in result.stderr


def test_explicit_repository_wins_over_detection(monkeypatch) -> None:
    def must_not_detect():
        raise AssertionError("detection must not run when a repository is given")

    monkeypatch.setattr("circa.cli.detect_repository", must_not_detect)
    which, run = _gh_run(RUN_LIST)

    with which, run as gh:
        result = runner.invoke(app, ["runs", "other/repo", "--json"])

    assert result.exit_code == 0, result.output
    assert gh.call_args.args[0][3:5] == ["--repo", "other/repo"]


def test_numeric_run_id_without_repo_uses_the_detected_repository(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _detected(monkeypatch)
    which, run = _gh_run("build\tstep\t2026-10-23T16:42:24Z ERROR: disk full on /dev/sda1\n")

    with which, run as gh, patch("circa.cli.OllamaProvider") as provider:
        provider.return_value.diagnose.return_value = None
        result = runner.invoke(app, ["investigate-log", "39988949831", "--json"])

    assert result.exit_code == 0, result.output
    assert gh.call_args.args[0][1:6] == ["run", "view", "39988949831", "--repo", "scmbuildrelease/CIRCA"]


def test_detection_failure_explains_how_to_pass_the_repository(monkeypatch) -> None:
    def no_repository():
        raise GitHubRunError("not inside a Git repository, so the repository cannot be detected; pass OWNER/REPO")

    monkeypatch.setattr("circa.cli.detect_repository", no_repository)

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 1
    assert "not inside a Git repository" in result.output and "pass OWNER/REPO" in result.output


# Terminal safety

STACK_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/build/app/test_io.py", line 42, in test_write\n'
    "    write_report(path)\n"
    "OSError: [Errno 28] No space left on device: '/dev/sda1'"
)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("\x1b[31mERROR\x1b[0m: build failed", "ERROR: build failed"),
        ("\x1b]0;pwned\x07ERROR: disk full", "ERROR: disk full"),
        ("\x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\", "click"),
        ("\x1b[2J\x1b[HERROR\x1b[1A\x1b[K", "ERROR"),
        ("\x1bPq#0;2;0;0;0\x1b\\ok", "ok"),
        ("\x9b31mERROR", "ERROR"),
        ("real error\rfake success", "real errorfake success"),
        ("bell\x07 null\x00 backspace\x08 del\x7f", "bell null backspace del"),
        ("file\u202etxt.exe", "filetxt.exe"),
        ("2026-10-23T16:42:24.2360059Z\tbuild\tERROR: x", "2026-10-23T16:42:24.2360059Z\tbuild\tERROR: x"),
        ("Fehler: Datei für 测试 nicht gefunden ✗ — café", "Fehler: Datei für 测试 nicht gefunden ✗ — café"),
        (STACK_TRACE, STACK_TRACE),
    ],
    ids=[
        "sgr-color",
        "osc-title",
        "osc-hyperlink",
        "cursor-and-erase",
        "dcs",
        "c1-csi",
        "carriage-return",
        "c0-and-delete",
        "bidi-override",
        "timestamp-and-tabs",
        "unicode",
        "stack-trace",
    ],
)
def test_safe_text_removes_terminal_control_and_keeps_content(raw: str, shown: str) -> None:
    assert safe_text(raw) == shown


def test_cli_output_is_sanitized_but_analysis_input_and_json_keep_the_original(tmp_path: Path) -> None:
    log = tmp_path / "ci.log"
    original = "2026-10-23T16:42:24Z \x1b]0;pwned\x07\x1b[31mERROR: No space left on device\x1b[0m on /dev/sda1\n"
    log.write_text(original, encoding="utf-8")

    with patch("circa.cli.OllamaProvider") as provider:
        provider.return_value.diagnose.return_value = None
        text = runner.invoke(app, ["investigate-log", str(log)])
        as_json = runner.invoke(app, ["investigate-log", str(log), "--json"])

    assert text.exit_code == 0 and as_json.exit_code == 0
    assert "\x1b" not in text.output and "\x07" not in text.output
    assert "ERROR: No space left on device on /dev/sda1" in text.stdout
    # Analysis saw the original log, and --json reports it unchanged.
    assert any("\x1b]0;pwned\x07" in line for line in json.loads(as_json.stdout)["evidence"])
    assert log.read_text(encoding="utf-8") == original


def test_github_log_evidence_is_sanitized_for_display(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    which, run = _gh_run("build\tstep\t2026-10-23T16:42:24Z \x1b]0;pwned\x07ERROR: disk full on /dev/sda1\n")

    with which, run, patch("circa.cli.OllamaProvider") as provider:
        provider.return_value.diagnose.return_value = None
        result = runner.invoke(app, ["investigate-log", "1", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "ERROR: disk full on /dev/sda1" in result.stdout


# Diagnostics


def _which(*missing: str):
    return lambda name: None if name in missing else f"/usr/bin/{name}"


def _checks(
    tmp_path: Path,
    *,
    missing: tuple[str, ...] = (),
    login: Exception | None = None,
    repository: Exception | None = None,
    models: list[str] | None = None,
    knowledge: Path | None = None,
) -> dict[str, Check]:
    def detect():
        if repository is not None:
            raise repository
        return "scmbuildrelease/CIRCA", ["origin"]

    with (
        patch("circa.doctor.shutil.which", side_effect=_which(*missing)),
        patch("circa.doctor.check_github_login", side_effect=login),
        patch("circa.doctor.detect_repository", side_effect=detect),
        patch.object(OllamaProvider, "installed_models", return_value=models),
        patch.object(OllamaProvider, "diagnose") as diagnose,
        patch.object(OllamaProvider, "explain") as explain,
    ):
        checks = {check.name: check for check in run_checks(knowledge)}
    diagnose.assert_not_called()
    explain.assert_not_called()
    return checks


def test_healthy_environment(tmp_path: Path) -> None:
    knowledge = tmp_path / "team.json"
    knowledge.write_text("[]\n")

    checks = _checks(tmp_path, models=["llama3.2:latest"], knowledge=knowledge)

    assert {name: check.status for name, check in checks.items()} == dict.fromkeys(checks, "ok")
    assert checks["Repository"].detail == "scmbuildrelease/CIRCA (from git remote origin)"
    assert checks["Ollama"].detail.endswith("model llama3.2 installed")


def test_missing_git_and_github_cli_are_optional(tmp_path: Path) -> None:
    checks = _checks(tmp_path, missing=("git", "gh"))

    assert checks["Git"].status == "optional" and "git-scm.com" in checks["Git"].detail
    assert checks["GitHub CLI"].status == "optional" and "gh auth login" in checks["GitHub CLI"].detail
    assert "GitHub login" not in checks


def test_unauthenticated_github_cli_says_how_to_log_in(tmp_path: Path) -> None:
    error = GitHubRunError("the GitHub CLI is not authenticated; run 'gh auth login' (You are not logged in)")

    checks = _checks(tmp_path, login=error)

    assert checks["GitHub login"].status == "optional"
    assert "run 'gh auth login'" in checks["GitHub login"].detail


def test_missing_and_invalid_knowledge_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")

    absent = _checks(tmp_path, knowledge=missing)["Knowledge file"]
    broken = _checks(tmp_path, knowledge=invalid)["Knowledge file"]

    assert absent.status == "optional" and f"circa init --knowledge {missing}" in absent.detail
    assert broken.status == "error" and "must contain a JSON list" in broken.detail
    assert not missing.exists()


def test_ollama_unavailable_or_missing_model_is_optional(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CIRCA_LLM_MODEL", raising=False)
    unreachable = _checks(tmp_path, models=None)["Ollama"]
    no_model = _checks(tmp_path, models=["qwen2.5:1.5b"])["Ollama"]

    assert unreachable.status == "optional" and "not reachable" in unreachable.detail
    assert no_model.status == "optional" and "ollama pull llama3.2" in no_model.detail
    assert "qwen2.5:1.5b" in no_model.detail


def test_installed_models_lists_tags_without_running_a_model() -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5:1.5b"}]}'

    with patch("circa.llm.urlopen", return_value=Response()) as urlopen:
        models = OllamaProvider(url="http://localhost:11434").installed_models()

    request = urlopen.call_args.args[0]
    assert models == ["llama3.2:latest", "qwen2.5:1.5b"]
    assert request.full_url == "http://localhost:11434/api/tags" and request.get_method() == "GET"
    with patch("circa.llm.urlopen", side_effect=OSError("refused")):
        assert OllamaProvider().installed_models() is None
    assert OllamaProvider(url="file:///etc/passwd").installed_models() is None


def test_doctor_exit_codes_change_nothing_and_never_print_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    token = "ghp_" + "b" * 36
    failing = subprocess.CompletedProcess([], 1, "", f"X github.com: token {token} is invalid\nrun gh auth login")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    before = sorted(path.name for path in tmp_path.iterdir())

    with (
        patch("circa.github_runs.shutil.which", side_effect=_which()),
        patch("circa.github_runs.subprocess.run", return_value=failing),
        patch.object(OllamaProvider, "installed_models", return_value=None),
    ):
        ready = runner.invoke(app, ["doctor"])
        broken = runner.invoke(app, ["doctor", "--knowledge", str(invalid)])

    assert ready.exit_code == 0, ready.output
    assert "not authenticated; run 'gh auth login'" in ready.output and "CIRCA is ready for local logs" in ready.output
    assert token not in ready.output and token not in broken.output
    assert broken.exit_code == 1 and "ERROR" in broken.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == before


# Initialization


def _quiet_checks():
    # GitHub is reported ready; the knowledge file is checked for real.
    return patch(
        "circa.cli.run_checks",
        side_effect=lambda knowledge: [
            Check("GitHub CLI", "ok", "installed"),
            Check("GitHub login", "ok", "logged in to github.com"),
            Check("Repository", "ok", "scmbuildrelease/CIRCA (from git remote origin)"),
            knowledge_check(knowledge),
        ],
    )


def test_init_creates_an_empty_knowledge_file_and_is_safe_to_rerun(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _quiet_checks():
        first = runner.invoke(app, ["init"])
        created = (tmp_path / "team" / "incidents.json").read_bytes()
        second = runner.invoke(app, ["init"])

    assert first.exit_code == 0, first.output
    assert "Created an empty knowledge file: team/incidents.json" in first.stdout
    assert json.loads(created) == []
    assert second.exit_code == 0 and "it was not changed" in second.stdout
    assert (tmp_path / "team" / "incidents.json").read_bytes() == created
    assert "circa runs\n" in first.stdout  # the repository was detected, so no OWNER/REPO placeholder
    assert "circa investigate-log --knowledge team/incidents.json" in first.stdout


def test_init_never_overwrites_existing_knowledge(tmp_path: Path) -> None:
    existing = tmp_path / "knowledge.json"
    existing.write_text(Path("team/incidents.json").read_text())
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json")
    before = existing.read_bytes(), invalid.read_bytes()

    with _quiet_checks():
        kept = runner.invoke(app, ["init", "--knowledge", str(existing)])
        broken = runner.invoke(app, ["init", "--knowledge", str(invalid)])

    assert kept.exit_code == 0 and "it was not changed" in kept.stdout
    assert broken.exit_code == 1 and "not valid UTF-8 JSON" in broken.stdout
    assert (existing.read_bytes(), invalid.read_bytes()) == before


def test_init_without_github_shows_login_and_placeholders(tmp_path: Path) -> None:
    checks = [
        Check("GitHub CLI", "optional", "not found; needed only for GitHub runs"),
        Check("Repository", "optional", "not inside a Git repository"),
    ]
    with patch("circa.cli.run_checks", return_value=checks):
        result = runner.invoke(app, ["init", "--knowledge", str(tmp_path / "a" / "b.json")])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "a" / "b.json").exists()
    assert "gh auth login" in result.stdout
    assert "circa runs OWNER/REPO" in result.stdout and "--repo OWNER/REPO" in result.stdout


def test_init_reports_an_uncreatable_knowledge_file(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")

    result = runner.invoke(app, ["init", "--knowledge", str(blocker / "team.json")])

    assert result.exit_code == 1 and "could not create" in result.output


# First-run messages and backward compatibility


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0 and result.stdout.startswith("circa ")


def test_missing_knowledge_file_explains_where_and_how(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"

    result = runner.invoke(app, ["knowledge", "list", "--knowledge", str(missing)])

    assert result.exit_code == 1
    assert f"knowledge file not found: {missing}" in result.output
    assert "circa init --knowledge" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["analyze", "examples/hardware_failure.json"],
        ["analyze-log", "examples/demo_disk_full.log"],
        ["analyze-log", "examples/demo_disk_full.log", "--json"],
        ["knowledge", "validate", "team/incidents.json"],
        ["knowledge", "list", "--knowledge", "team/incidents.json"],
        ["benchmark"],
    ],
)
def test_existing_commands_still_work(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output


def test_version_is_reported_when_circa_is_not_installed_as_a_package() -> None:
    from importlib.metadata import PackageNotFoundError

    from circa.doctor import circa_version

    with patch("circa.doctor.version", side_effect=PackageNotFoundError("circa")):
        assert circa_version() == "unknown (not installed as a package)"
