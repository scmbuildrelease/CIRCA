"""Read-only environment checks for ``circa doctor`` and ``circa init``.

Nothing here writes files, calls a language model, or prints credentials.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from .github_runs import GitHubRunError, check_github_login, detect_repository
from .knowledge_store import KnowledgeError, load_knowledge
from .llm import OllamaProvider

Status = Literal["ok", "optional", "error"]
DEFAULT_KNOWLEDGE = Path("team/incidents.json")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status  # "error" only for problems that break local analysis
    detail: str


def circa_version() -> str:
    try:
        return version("circa")
    except PackageNotFoundError:
        return "unknown (not installed as a package)"


def knowledge_check(path: Path | None) -> Check:
    if path is None:
        if DEFAULT_KNOWLEDGE.is_file():
            return Check(
                "Knowledge file",
                "optional",
                f"not specified; found {DEFAULT_KNOWLEDGE} (pass --knowledge {DEFAULT_KNOWLEDGE})",
            )
        return Check(
            "Knowledge file", "optional", "not specified; only packaged examples are matched (create one: circa init)"
        )
    if not path.exists():
        return Check(
            "Knowledge file", "optional", f"{path} does not exist yet (create it: circa init --knowledge {path})"
        )
    try:
        incidents = load_knowledge(path).incidents
    except KnowledgeError as error:
        return Check("Knowledge file", "error", str(error))
    counts = {
        status: sum(incident.status == status for incident in incidents) for status in ("verified", "pending_review")
    }
    return Check(
        "Knowledge file",
        "ok",
        f"{path}: valid; {counts['verified']} verified, {counts['pending_review']} pending_review",
    )


def run_checks(knowledge: Path | None = None) -> list[Check]:
    """Check what CIRCA needs; GitHub and Ollama are optional and never block local log analysis."""

    python = ".".join(map(str, sys.version_info[:3]))
    checks = [
        Check("Python", "ok" if sys.version_info >= (3, 12) else "error", f"{python} (3.12+ required)"),
        Check("CIRCA", "ok", circa_version()),
    ]
    git = shutil.which("git")
    checks.append(
        Check("Git", "ok", "installed")
        if git
        else Check("Git", "optional", "not found; needed only to detect the repository (install: https://git-scm.com)")
    )
    if shutil.which("gh") is None:
        checks.append(
            Check(
                "GitHub CLI",
                "optional",
                "not found; needed only for GitHub runs (install: https://cli.github.com, then: gh auth login)",
            )
        )
    else:
        checks.append(Check("GitHub CLI", "ok", "installed"))
        try:
            check_github_login()
            checks.append(Check("GitHub login", "ok", "logged in to github.com"))
        except GitHubRunError as error:
            checks.append(Check("GitHub login", "optional", f"{error}"))
    try:
        repository, remotes = detect_repository()
        checks.append(Check("Repository", "ok", f"{repository} (from git remote {', '.join(remotes)})"))
    except GitHubRunError as error:
        checks.append(Check("Repository", "optional", str(error)))
    checks.append(knowledge_check(knowledge))
    provider = OllamaProvider(timeout=2.0)
    models = provider.installed_models()
    if models is None:
        detail = f"not reachable at {provider.url}; optional, used only for new diagnoses (https://ollama.com)"
        checks.append(Check("Ollama", "optional", detail))
    elif provider.model in models or f"{provider.model}:latest" in models:
        checks.append(Check("Ollama", "ok", f"{provider.url}; model {provider.model} installed"))
    else:
        detail = f"reachable, but model {provider.model} is not installed (run: ollama pull {provider.model}, or set CIRCA_LLM_MODEL to one of: {', '.join(models) or 'none'})"
        checks.append(Check("Ollama", "optional", detail))
    return checks
