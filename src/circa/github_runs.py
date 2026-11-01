"""Retrieve failed-step logs for a GitHub Actions run through the GitHub CLI.

``gh`` is optional: it is needed only when a run URL or run ID is investigated,
and it uses the user's existing ``gh auth login`` session. No token is read or
printed by CIRCA.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404  # fixed argument list with shell=False
from dataclasses import asdict, dataclass
from typing import Any

from .redaction import redact_secrets

GH_TIMEOUT_SECONDS = 120.0
# "." and ".." are not valid repository names, so they are never passed to gh.
_RUN_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>(?!\.{1,2}/)[A-Za-z0-9._-]{1,100})"
    r"/actions/runs/(?P<run_id>\d{1,20})(?:/attempts/\d{1,5})?/?(?:[?#].*)?$"
)
_REPOSITORY = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>(?!\.{1,2}$)[A-Za-z0-9._-]{1,100})$")
_REPOSITORY_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>(?!\.{1,2}(?:\.git)?/?$)[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_RUN_LIMIT = 100
_RUN_FIELDS = "databaseId,workflowName,headBranch,conclusion,createdAt,url,displayTitle"
_RUN_ID = re.compile(r"^\d{1,20}$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class GitHubRunError(ValueError):
    """A run reference is invalid or its logs cannot be retrieved."""


@dataclass(frozen=True)
class GitHubRun:
    owner: str
    repo: str
    run_id: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/actions/runs/{self.run_id}"


@dataclass(frozen=True)
class FailedRun:
    run_id: str
    workflow: str
    branch: str
    conclusion: str
    created_at: str
    url: str
    title: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def is_repository_url(value: str) -> bool:
    return _REPOSITORY_URL.match(value) is not None


def parse_repository(value: str) -> str:
    """Return ``OWNER/REPO`` for ``OWNER/REPO`` or ``https://github.com/OWNER/REPO``."""

    match = _REPOSITORY.match(value) or _REPOSITORY_URL.match(value)
    if match is None:
        raise GitHubRunError(f"repository must look like OWNER/REPO or https://github.com/OWNER/REPO, not '{value}'")
    return f"{match['owner']}/{match['repo']}"


def is_run_reference(value: str) -> bool:
    """Whether ``value`` is meant as a run (a URL or a bare run ID) rather than a file path."""

    return value.startswith(("http://", "https://")) or _RUN_ID.match(value) is not None


def parse_run_reference(value: str, repository: str | None = None) -> GitHubRun:
    """Parse a GitHub Actions run URL, or a numeric run ID with an explicit ``OWNER/REPO``."""

    if _RUN_ID.match(value):
        if repository is None:
            raise GitHubRunError(f"run ID {value} needs --repo OWNER/REPO (or pass the full run URL)")
        owner, repo = parse_repository(repository).split("/")
        if int(value) == 0:
            raise GitHubRunError("run ID must be a positive number")
        return GitHubRun(owner, repo, value)
    match = _RUN_URL.match(value)
    if match is None:
        raise GitHubRunError(f"unsupported URL '{value}'; expected https://github.com/OWNER/REPO/actions/runs/RUN_ID")
    run = GitHubRun(match["owner"], match["repo"], match["run_id"])
    if repository is not None and parse_repository(repository).lower() != f"{run.owner}/{run.repo}".lower():
        raise GitHubRunError(f"--repo {repository} does not match the run URL's repository {run.owner}/{run.repo}")
    return run


def _stderr_summary(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    safe = [redact_secrets(line)[:300] for line in lines[:3]]
    return " / ".join(safe) or "no error output"


def _gh(arguments: list[str], subject: str, timeout: float) -> str:
    """Run ``gh`` with a fixed argument list and return stdout, or raise an actionable error."""

    gh = shutil.which("gh")
    if gh is None:
        raise GitHubRunError(
            "GitHub CLI 'gh' was not found; install it from https://cli.github.com and run 'gh auth login'"
        )
    command = [gh, *arguments]
    # Never prompt: a missing login must fail instead of waiting for input.
    environment = os.environ | {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    try:
        completed = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # CI logs can contain stray binary output
            timeout=timeout,
            shell=False,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise GitHubRunError(
            f"'gh {arguments[0]} {arguments[1]}' timed out after {timeout:g}s for {subject}"
        ) from error
    except OSError as error:
        raise GitHubRunError(f"could not run the GitHub CLI: {error.strerror or error}") from error
    if completed.returncode != 0:
        detail = _stderr_summary(completed.stderr)
        lowered = completed.stderr.lower()
        if "gh auth login" in lowered or "not logged" in lowered or "authentication" in lowered:
            raise GitHubRunError(f"the GitHub CLI is not authenticated; run 'gh auth login' ({detail})")
        if "404" in lowered or "not found" in lowered or "could not resolve" in lowered:
            raise GitHubRunError(f"{subject} was not found or the repository is not accessible ({detail})")
        raise GitHubRunError(
            f"'gh {arguments[0]} {arguments[1]}' failed with exit code {completed.returncode} for {subject} ({detail})"
        )
    return completed.stdout


def fetch_failed_log(run: GitHubRun, timeout: float = GH_TIMEOUT_SECONDS) -> str:
    """Return the run's failed-step logs (``gh run view --log-failed``) with ANSI colors removed."""

    arguments = ["run", "view", run.run_id, "--repo", f"{run.owner}/{run.repo}", "--log-failed"]
    log = _ANSI.sub("", _gh(arguments, f"run {run.url}", timeout))
    if not log.strip():
        raise GitHubRunError(
            f"run {run.url} returned no failed-step logs (it may have succeeded, still be running, or its logs expired)"
        )
    return log


def _text(record: dict[str, Any], key: str) -> str:
    # Names and branches come from GitHub; drop control characters before printing them.
    return _CONTROL.sub("", str(record.get(key) or ""))


def list_failed_runs(repository: str, limit: int = 10, timeout: float = GH_TIMEOUT_SECONDS) -> list[FailedRun]:
    """Return the repository's most recent failed workflow runs, newest first (``gh run list``)."""

    if not 1 <= limit <= MAX_RUN_LIMIT:
        raise GitHubRunError(f"limit must be between 1 and {MAX_RUN_LIMIT}, not {limit}")
    repository = parse_repository(repository)
    arguments = ["run", "list", "--repo", repository, "--status", "failure", "--limit", str(limit)]
    output = _gh([*arguments, "--json", _RUN_FIELDS], f"repository {repository}", timeout)
    try:
        records = json.loads(output)
    except json.JSONDecodeError as error:
        raise GitHubRunError(f"'gh run list' returned invalid JSON for {repository}") from error
    if not isinstance(records, list):
        raise GitHubRunError(f"'gh run list' returned unexpected output for {repository}")
    runs = [
        FailedRun(
            run_id=str(record["databaseId"]),
            workflow=_text(record, "workflowName"),
            branch=_text(record, "headBranch"),
            conclusion=_text(record, "conclusion"),
            created_at=_text(record, "createdAt"),
            url=_text(record, "url"),
            title=_text(record, "displayTitle"),
        )
        for record in records
        if isinstance(record, dict) and _RUN_ID.match(str(record.get("databaseId", "")))
    ]
    if not runs:
        raise GitHubRunError(f"no failed workflow runs were found in {repository}")
    return runs


_REMOTE_URL = re.compile(
    r"^(?:https://(?:[^@/\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com(?::\d{1,5})?/)"
    r"(?P<path>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_REMOTE_HOST = re.compile(r"^(?:[a-z+]+://)?(?:[^@/\s]+@)?(?P<host>[^:/\s]+)", re.IGNORECASE)


def github_repository_from_remote(url: str) -> str | None:
    """Return ``OWNER/REPO`` for a github.com remote URL (HTTPS, SSH, or scp-style), else ``None``."""

    match = _REMOTE_URL.match(url.strip())
    if match is None:
        return None
    try:
        return parse_repository(match["path"])
    except GitHubRunError:
        return None


def _remote_host(url: str) -> str:
    # Only the host is ever shown: a remote URL may embed a username or token.
    match = _REMOTE_HOST.match(url.strip())
    return match["host"] if match else "unknown host"


def detect_repository(directory: str | os.PathLike[str] | None = None) -> tuple[str, list[str]]:
    """Return the GitHub ``OWNER/REPO`` of the current checkout and the remotes that name it.

    Raises :class:`GitHubRunError` when Git is missing, the directory is not a Git
    checkout, no remote points to github.com, or remotes name different repositories.
    """

    git = shutil.which("git")
    if git is None:
        raise GitHubRunError("Git was not found, so the repository cannot be detected; pass OWNER/REPO")
    command = [git, "remote", "-v"] if directory is None else [git, "-C", os.fspath(directory), "remote", "-v"]
    try:
        completed = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            check=False,
            env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubRunError(
            f"could not run Git to detect the repository ({type(error).__name__}); pass OWNER/REPO"
        ) from error
    if completed.returncode != 0:
        raise GitHubRunError("not inside a Git repository, so the repository cannot be detected; pass OWNER/REPO")
    remotes: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])
    if not remotes:
        raise GitHubRunError("this Git repository has no remotes; pass OWNER/REPO")
    found = {name: github_repository_from_remote(url) for name, url in remotes.items()}
    github = {name: repository for name, repository in found.items() if repository is not None}
    if not github:
        hosts = ", ".join(f"{name} ({_remote_host(url)})" for name, url in remotes.items())
        raise GitHubRunError(f"no Git remote points to github.com (remotes: {hosts}); CIRCA supports github.com only")
    repositories = {repository.lower(): repository for repository in github.values()}
    if len(repositories) > 1:
        choices = ", ".join(f"{name} -> {repository}" for name, repository in github.items())
        raise GitHubRunError(f"Git remotes name different GitHub repositories ({choices}); pass OWNER/REPO explicitly")
    return next(iter(repositories.values())), sorted(github)


def check_github_login(timeout: float = 15.0) -> None:
    """Raise :class:`GitHubRunError` unless ``gh`` is installed and logged in to github.com.

    Only the exit status is used; ``gh auth status`` output is never shown.
    """

    _gh(["auth", "status", "--hostname", "github.com"], "github.com", timeout)
