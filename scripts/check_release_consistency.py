"""Check that release version references agree with pyproject.toml.

``project.version`` in pyproject.toml is the only authoritative version. This script checks,
without network access:

- the version is a plain ``X.Y.Z`` release and its tag is ``vX.Y.Z``;
- the newest CHANGELOG.md release heading is that version, and every release tag has a heading;
- the documentation's current-release references (listed in CURRENT_REFERENCES) name it;
- every ``git tag -a`` example is pushed with the same tag and isn't older than the release;
- release tags in this checkout aren't newer than the version, and ``vX.Y.Z``, if present,
  points to a commit whose pyproject.toml has that version. A checkout without tags (a fresh
  shallow clone) skips the tag checks with a note.

Historical versions (older CHANGELOG entries, "0.2.1 or earlier", earlier discrepancies) are
not checked: only the explicit current-release patterns below are.

``--online`` also checks that PyPI's latest release and GitHub's latest release are this
version. It needs network access but no credentials.

Usage (from the repository root):

    python scripts/check_release_consistency.py [--online]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404  # fixed git argument lists, no shell
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
_VERSION = r"\d+\.\d+\.\d+"
_RELEASE = re.compile(rf"^{_VERSION}$")
_HEADING = re.compile(r"^## (?P<title>.+)$", re.MULTILINE)
_RELEASE_HEADING = re.compile(rf"^(?P<version>{_VERSION}) — \d{{4}}-\d{{2}}-\d{{2}}$")
# Explicit references to the current release; each must occur at least once and name the version.
CURRENT_REFERENCES = [
    ("README.md", rf"latest release, currently (?P<version>{_VERSION})", "PyPI install comment"),
    ("README.md", rf"CIRCA\.git@v(?P<version>{_VERSION})", "GitHub tag install"),
    ("README.md", rf"circa_ci-(?P<version>{_VERSION})-py3-none-any\.whl", "local wheel install"),
    ("docs/EXTERNAL_SETUP.md", rf"It describes\s+the (?P<version>{_VERSION}) release", "described release"),
]
# Markdown files whose ``git tag -a`` and ``git push origin`` examples are checked.
EXAMPLE_FILES = ["README.md", "CONTRIBUTING.md", "docs/EXTERNAL_SETUP.md", "docs/INTERVIEW_DEMO.md"]
_TAG_EXAMPLE = re.compile(rf"git tag -a v(?P<version>{_VERSION})(?: -m \"(?P<message>[^\"]*)\")?")
_PUSH_EXAMPLE = re.compile(r"git push origin (?P<ref>\S+)")
_MESSAGE_VERSION = re.compile(rf"v?{_VERSION}")


class Report:
    """Errors fail the check; notes explain what was skipped or not yet released."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.notes: list[str] = []


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def read_project(root: Path) -> dict[str, Any]:
    with (root / "pyproject.toml").open("rb") as handle:
        project: dict[str, Any] = tomllib.load(handle)["project"]
    return project


def check_version(version: str, report: Report) -> None:
    if not _RELEASE.match(version):
        report.errors.append(f"pyproject.toml version '{version}' is not a plain X.Y.Z release")


def changelog_versions(root: Path, report: Report) -> list[str]:
    """Return the CHANGELOG release headings in order; an "Unreleased" heading is allowed first."""

    versions: list[str] = []
    for match in _HEADING.finditer((root / "CHANGELOG.md").read_text(encoding="utf-8")):
        title = match["title"].strip()
        if title.lower() == "unreleased" and not versions:
            continue
        release = _RELEASE_HEADING.match(title)
        if release is None:
            report.errors.append(f"CHANGELOG.md heading '## {title}' is not 'X.Y.Z — YYYY-MM-DD'")
        else:
            versions.append(release["version"])
    return versions


def check_changelog(versions: list[str], version: str, report: Report) -> None:
    if not versions:
        report.errors.append("CHANGELOG.md has no release heading")
    elif versions[0] != version:
        report.errors.append(f"CHANGELOG.md's newest release is {versions[0]}, but pyproject.toml says {version}")


def check_current_references(root: Path, version: str, report: Report) -> None:
    for name, pattern, label in CURRENT_REFERENCES:
        path = root / name
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        found = [match["version"] for match in re.finditer(pattern, text)]
        if not found:
            report.errors.append(f"{name}: {label} reference to the current version not found")
        for value in found:
            if value != version:
                report.errors.append(f"{name}: {label} names {value}, but pyproject.toml says {version}")


def check_tag_examples(root: Path, version: str, report: Report) -> None:
    for name in EXAMPLE_FILES:
        path = root / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            tag = _TAG_EXAMPLE.search(line)
            if tag is None:
                continue
            example = f"v{tag['version']}"
            where = f"{name}:{number}"
            if _key(tag["version"]) < _key(version):
                report.errors.append(f"{where}: tag example {example} is older than the current version {version}")
            message = tag["message"] or ""
            if any(value.lstrip("v") != tag["version"] for value in _MESSAGE_VERSION.findall(message)):
                report.errors.append(f"{where}: tag message '{message}' doesn't match {example}")
            # The push belongs to the same example when it follows within a few lines.
            for offset, following in enumerate(lines[number : number + 3], start=number + 1):
                push = _PUSH_EXAMPLE.search(following)
                if push is not None:
                    if push["ref"] != example:
                        report.errors.append(f"{name}:{offset}: pushes {push['ref']} after tagging {example}")
                    break


def _git(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(  # nosec B603 B607  # fixed argument list, no shell
            ["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def check_git_tags(root: Path, version: str, changelog: list[str], report: Report) -> None:
    listed = _git(root, "tag", "--list", "v*")
    if listed is None:
        report.notes.append("not a git checkout (or git is unavailable); tag checks skipped")
        return
    tags = sorted((tag[1:] for tag in listed.split() if _RELEASE.match(tag[1:])), key=_key)
    if not tags:
        report.notes.append("no release tags in this checkout (fetch them with 'git fetch --tags'); tag checks skipped")
        return
    newest = tags[-1]
    if _key(newest) > _key(version):
        report.errors.append(f"tag v{newest} is newer than the pyproject.toml version {version}")
    for tag in tags:
        if tag not in changelog:
            report.errors.append(f"tag v{tag} has no CHANGELOG.md release heading")
    if version not in tags:
        report.notes.append(f"v{version} is not tagged yet (unreleased)")
        return
    tagged = _git(root, "show", f"v{version}:pyproject.toml")
    tagged_version = tomllib.loads(tagged)["project"]["version"] if tagged is not None else None
    if tagged_version != version:
        report.errors.append(f"tag v{version} points to a commit whose pyproject.toml says {tagged_version}")


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "circa-release-check"})
    with urlopen(request, timeout=30) as response:  # nosec B310  # fixed https URLs
        return json.load(response)


def check_online(project: dict[str, Any], version: str, report: Report, fetch: Callable[[str], Any]) -> None:
    name = project["name"]
    try:
        pypi = fetch(f"https://pypi.org/pypi/{name}/json")
        if pypi["info"]["version"] != version:
            report.errors.append(f"PyPI's latest {name} release is {pypi['info']['version']}, not {version}")
    except Exception as error:  # noqa: BLE001 - any failure means PyPI could not be verified
        report.errors.append(f"could not verify PyPI: {error}")
    homepage = project.get("urls", {}).get("Homepage", "")
    repository = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)", homepage)
    if repository is None:
        report.errors.append("project.urls.Homepage is not a github.com repository URL; GitHub not verified")
        return
    try:
        release = fetch(f"https://api.github.com/repos/{repository[1]}/releases/latest")
        if release["tag_name"] != f"v{version}":
            report.errors.append(f"GitHub's latest release is {release['tag_name']}, not v{version}")
    except Exception as error:  # noqa: BLE001 - any failure means GitHub could not be verified
        report.errors.append(f"could not verify the GitHub release: {error}")


def run(root: Path, *, online: bool = False, fetch: Callable[[str], Any] = _fetch_json) -> Report:
    report = Report()
    project = read_project(root)
    version = str(project["version"])
    check_version(version, report)
    if report.errors:
        return report  # the other checks compare X.Y.Z versions
    changelog = changelog_versions(root, report)
    check_changelog(changelog, version, report)
    check_current_references(root, version, report)
    check_tag_examples(root, version, report)
    check_git_tags(root, version, changelog, report)
    if online:
        check_online(project, version, report, fetch)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that release version references agree with pyproject.toml.")
    parser.add_argument("--online", action="store_true", help="also check the latest PyPI and GitHub releases")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    arguments = parser.parse_args(argv)
    report = run(arguments.root, online=arguments.online)
    version = read_project(arguments.root)["version"]
    for note in report.notes:
        print(f"note: {note}")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    if report.errors:
        print(f"Release consistency: {len(report.errors)} problem(s) for version {version}", file=sys.stderr)
        return 1
    print(f"Release consistency: OK for version {version} (tag v{version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
