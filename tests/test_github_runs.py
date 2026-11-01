import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from circa.github_runs import (
    FailedRun,
    GitHubRun,
    GitHubRunError,
    fetch_failed_log,
    is_run_reference,
    list_failed_runs,
    parse_repository,
    parse_run_reference,
)

RUN_URL = "https://github.com/scmbuildrelease/CIRCA/actions/runs/39988949831"
RUN = GitHubRun("scmbuildrelease", "CIRCA", "39988949831")


@pytest.mark.parametrize(
    "url",
    [RUN_URL, RUN_URL + "/", RUN_URL + "/attempts/2", RUN_URL + "?pr=1", RUN_URL + "#summary"],
    ids=["plain", "slash", "attempt", "query", "fragment"],
)
def test_run_urls_parse_owner_repository_and_run_id(url: str) -> None:
    assert parse_run_reference(url) == RUN
    assert parse_run_reference(url).url == RUN_URL


def test_numeric_run_id_requires_an_explicit_repository() -> None:
    assert parse_run_reference("39988949831", "scmbuildrelease/CIRCA") == RUN
    with pytest.raises(GitHubRunError, match="needs --repo"):
        parse_run_reference("39988949831")
    with pytest.raises(GitHubRunError, match="OWNER/REPO"):
        parse_run_reference("39988949831", "not a repo")
    with pytest.raises(GitHubRunError, match="does not match"):
        parse_run_reference(RUN_URL, "someone/else")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/scmbuildrelease/CIRCA/-/pipelines/1",
        "https://github.com/scmbuildrelease/CIRCA",
        "https://github.com/scmbuildrelease/CIRCA/pull/1",
        "https://github.com/scmbuildrelease/CIRCA/actions/runs/abc",
        "http://github.com/scmbuildrelease/CIRCA/actions/runs/1",
        "https://github.com/scmbuildrelease/CIRCA/actions/runs/1;rm -rf ~",
        "https://github.com.evil.example/a/b/actions/runs/1",
    ],
    ids=["gitlab", "repository", "pull-request", "non-numeric", "http", "injection", "lookalike-host"],
)
def test_invalid_or_unsupported_urls_are_rejected(url: str) -> None:
    assert is_run_reference(url)
    with pytest.raises(GitHubRunError, match="unsupported URL"):
        parse_run_reference(url)


def test_local_paths_are_not_run_references() -> None:
    assert not is_run_reference("examples/demo_disk_full.log")
    assert is_run_reference("39988949831")


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _fetch(result: object):
    side_effect = result if isinstance(result, BaseException) else None
    return_value = None if side_effect else result
    with (
        patch("circa.github_runs.shutil.which", return_value="/usr/bin/gh"),
        patch("circa.github_runs.subprocess.run", return_value=return_value, side_effect=side_effect) as run,
    ):
        try:
            return fetch_failed_log(RUN), run
        except GitHubRunError as error:
            return error, run


def test_failed_log_is_fetched_without_a_shell_and_colors_are_removed() -> None:
    log, run = _fetch(_completed(stdout="Lint (Ruff)\tRuff format check\t\x1b[31merror\x1b[0m\n"))

    assert log == "Lint (Ruff)\tRuff format check\terror\n"
    args, kwargs = run.call_args
    assert args[0] == ["/usr/bin/gh", "run", "view", "39988949831", "--repo", "scmbuildrelease/CIRCA", "--log-failed"]
    assert kwargs["shell"] is False
    assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"


def test_missing_github_cli_is_reported() -> None:
    with (
        patch("circa.github_runs.shutil.which", return_value=None),
        patch("circa.github_runs.subprocess.run") as run,
        pytest.raises(GitHubRunError, match="'gh' was not found"),
    ):
        fetch_failed_log(RUN)
    run.assert_not_called()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_completed(4, stderr="To get started with GitHub CLI, please run:  gh auth login"), "not authenticated"),
        (_completed(1, stderr="failed to get run: HTTP 404: Not Found"), "not found or the repository is not"),
        (_completed(1, stderr="run 39988949831 is still in progress"), "failed with exit code 1"),
        (subprocess.TimeoutExpired(cmd="gh", timeout=120), "timed out after 120s"),
        (FileNotFoundError(2, "No such file or directory"), "could not run the GitHub CLI"),
        (_completed(0, stdout="  \n"), "no failed-step logs"),
    ],
    ids=["unauthenticated", "inaccessible", "nonzero", "timeout", "os-error", "empty"],
)
def test_github_cli_failures_raise_clear_errors(result: object, message: str) -> None:
    error, _ = _fetch(result)

    assert isinstance(error, GitHubRunError)
    assert message in str(error)


def test_error_output_never_echoes_credentials() -> None:
    token = "ghp_" + "a" * 36
    error, _ = _fetch(_completed(1, stderr=f"request failed with header Authorization: token {token}"))

    assert isinstance(error, GitHubRunError)
    assert token not in str(error) and "Authorization: token [REDACTED]" in str(error)


GH_RUNS = [
    {
        "databaseId": 39988949831,
        "workflowName": "CI",
        "headBranch": "main",
        "conclusion": "failure",
        "createdAt": "2026-10-23T16:42:04Z",
        "url": RUN_URL,
        "displayTitle": "Add CI quality gate",
    },
    {
        "databaseId": 39859422492,
        "workflowName": "CI\x1b[2J",
        "headBranch": "feature/x",
        "conclusion": "failure",
        "createdAt": "2026-10-22T21:42:34Z",
        "url": "https://github.com/scmbuildrelease/CIRCA/actions/runs/39859422492",
        "displayTitle": "Phase 9.1",
    },
]


def _list(result: object, repository: str = "scmbuildrelease/CIRCA", limit: int = 10):
    side_effect = result if isinstance(result, BaseException) else None
    return_value = None if side_effect else result
    with (
        patch("circa.github_runs.shutil.which", return_value="/usr/bin/gh"),
        patch("circa.github_runs.subprocess.run", return_value=return_value, side_effect=side_effect) as run,
    ):
        try:
            return list_failed_runs(repository, limit), run
        except GitHubRunError as error:
            return error, run


@pytest.mark.parametrize(
    "value",
    [
        "scmbuildrelease/CIRCA",
        "https://github.com/scmbuildrelease/CIRCA",
        "https://github.com/scmbuildrelease/CIRCA.git/",
    ],
)
def test_repository_names_and_urls_parse(value: str) -> None:
    assert parse_repository(value) == "scmbuildrelease/CIRCA"


@pytest.mark.parametrize(
    "value", ["CIRCA", "a/b/c", "https://gitlab.com/a/b", "https://github.com/a/b/actions", "-a/b", "a/b; ls"]
)
def test_invalid_repositories_are_rejected(value: str) -> None:
    with pytest.raises(GitHubRunError, match="OWNER/REPO"):
        parse_repository(value)


def test_failed_runs_are_listed_with_their_fields_and_without_a_shell() -> None:
    runs, gh = _list(_completed(stdout=json.dumps(GH_RUNS)), "https://github.com/scmbuildrelease/CIRCA", 2)

    assert [run.run_id for run in runs] == ["39988949831", "39859422492"]
    assert runs[0] == FailedRun(
        "39988949831", "CI", "main", "failure", "2026-10-23T16:42:04Z", RUN_URL, "Add CI quality gate"
    )
    assert runs[1].workflow == "CI[2J"  # terminal control characters from GitHub are removed
    args, kwargs = gh.call_args
    assert args[0][:9] == [
        "/usr/bin/gh",
        "run",
        "list",
        "--repo",
        "scmbuildrelease/CIRCA",
        "--status",
        "failure",
        "--limit",
        "2",
    ]
    assert args[0][9] == "--json"
    assert kwargs["shell"] is False and kwargs["timeout"] == 120.0


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_out_of_range_limits_are_rejected_before_calling_gh(limit: int) -> None:
    error, gh = _list(_completed(stdout="[]"), limit=limit)

    assert isinstance(error, GitHubRunError) and "between 1 and 100" in str(error)
    gh.assert_not_called()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_completed(stdout="[]"), "no failed workflow runs were found in scmbuildrelease/CIRCA"),
        (_completed(stdout="not json"), "invalid JSON"),
        (_completed(stdout='{"runs": []}'), "unexpected output"),
        (_completed(4, stderr="To get started with GitHub CLI, please run:  gh auth login"), "not authenticated"),
        (_completed(1, stderr="failed to get runs: HTTP 404: Not Found"), "repository scmbuildrelease/CIRCA was not"),
        (subprocess.TimeoutExpired(cmd="gh", timeout=120), "'gh run list' timed out"),
    ],
    ids=["no-failed-runs", "invalid-json", "not-a-list", "unauthenticated", "inaccessible", "timeout"],
)
def test_listing_failures_raise_clear_errors(result: object, message: str) -> None:
    error, _ = _list(result)

    assert isinstance(error, GitHubRunError)
    assert message in str(error)


def test_zero_run_id_is_rejected() -> None:
    with pytest.raises(GitHubRunError, match="positive"):
        parse_run_reference("0", "scmbuildrelease/CIRCA")


def test_invalid_utf8_in_a_real_gh_log_is_replaced_not_a_crash(tmp_path) -> None:
    # A real subprocess (no network): a stand-in gh prints a log with bytes that are not UTF-8.
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'build\\tstep\\tERROR: disk full \\xff\\xfe on /dev/sda1\\n')\n"
    )
    fake_gh.chmod(0o755)

    with patch("circa.github_runs.shutil.which", return_value=str(fake_gh)):
        log = fetch_failed_log(RUN)

    assert log == "build\tstep\tERROR: disk full �� on /dev/sda1\n"


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_repository_names_are_rejected(name: str) -> None:
    with pytest.raises(GitHubRunError, match="OWNER/REPO"):
        parse_repository(f"scmbuildrelease/{name}")
    with pytest.raises(GitHubRunError, match="OWNER/REPO"):
        parse_repository(f"https://github.com/scmbuildrelease/{name}")
    with pytest.raises(GitHubRunError, match="unsupported URL"):
        parse_run_reference(f"https://github.com/scmbuildrelease/{name}/actions/runs/1")
    with pytest.raises(GitHubRunError, match="OWNER/REPO"):
        parse_run_reference("1", f"scmbuildrelease/{name}")
    assert parse_repository("scmbuildrelease/.github") == "scmbuildrelease/.github"


def test_repo_option_may_be_a_repository_url() -> None:
    repository_url = "https://github.com/scmbuildrelease/CIRCA"

    assert parse_run_reference("39988949831", repository_url) == RUN
    assert parse_run_reference(RUN_URL, repository_url) == RUN
    assert parse_run_reference(RUN_URL, "SCMBuildRelease/circa") == RUN
