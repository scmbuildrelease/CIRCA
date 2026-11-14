import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_release_consistency")

PYPROJECT = """[project]
name = "circa-ci"
version = "{version}"

[project.urls]
Homepage = "https://github.com/scmbuildrelease/CIRCA"
"""
CHANGELOG = """# Changelog

## {version} — 2026-11-03

- Current release.

## 0.2.2 — 2026-11-01

- An older release; it is history, not a mismatch.
"""
README = """python -m pip install circa-ci        # latest release, currently {version}
If `circa` 0.2.1 or earlier is installed from GitHub, uninstall it first.
python -m pip install "git+https://github.com/scmbuildrelease/CIRCA.git@v{version}"
python -m pip install dist/circa_ci-{version}-py3-none-any.whl
"""
SETUP = """It describes
the {version} release (2026-11-03).

```bash
git tag -a v0.3.1 -m "CIRCA v0.3.1"   # must equal "v" + project.version
git push origin v0.3.1
```

1. The README for 0.2.2 still installed `v0.2.1`. Fixed for 0.3.0.
"""


def _repository(path: Path, version: str = "0.3.0", **overrides: str) -> Path:
    files = {
        "pyproject.toml": PYPROJECT,
        "CHANGELOG.md": CHANGELOG,
        "README.md": README,
        "docs/EXTERNAL_SETUP.md": SETUP,
    }
    for name, template in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(overrides.get(name, template).format(version=version), encoding="utf-8")
    return path


def _git(path: Path, *arguments: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(path), *identity, *arguments], check=True, capture_output=True)


def _commit_and_tag(path: Path, *tags: str) -> None:
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "release")
    for tag in tags:
        _git(path, "tag", "-a", tag, "-m", tag)


def test_matching_versions_pass(tmp_path: Path) -> None:
    _commit_and_tag(_repository(tmp_path), "v0.2.2", "v0.3.0")

    report = check.run(tmp_path)

    assert report.errors == []
    assert report.notes == []


def test_an_untagged_new_version_is_reported_as_unreleased_not_as_an_error(tmp_path: Path) -> None:
    _commit_and_tag(_repository(tmp_path, "0.3.1"), "v0.3.0")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG.format(version="0.3.1") + "\n## 0.3.0 — 2026-11-03\n")

    report = check.run(tmp_path)

    assert report.errors == []
    assert report.notes == ["v0.3.1 is not tagged yet (unreleased)"]


def test_a_tag_newer_than_the_package_version_fails(tmp_path: Path) -> None:
    _commit_and_tag(_repository(tmp_path), "v0.3.0", "v0.4.0")

    errors = check.run(tmp_path).errors

    assert "tag v0.4.0 is newer than the pyproject.toml version 0.3.0" in errors
    assert "tag v0.4.0 has no CHANGELOG.md release heading" in errors


def test_a_tag_on_a_commit_with_another_version_fails(tmp_path: Path) -> None:
    _repository(tmp_path, "0.2.9")
    _commit_and_tag(tmp_path, "v0.3.0")
    _repository(tmp_path, "0.3.0")

    errors = check.run(tmp_path).errors

    assert errors == ["tag v0.3.0 points to a commit whose pyproject.toml says 0.2.9"]


def test_missing_tags_in_a_fresh_checkout_are_skipped_with_a_note(tmp_path: Path) -> None:
    _commit_and_tag(_repository(tmp_path))

    report = check.run(tmp_path)

    assert report.errors == []
    assert report.notes == ["no release tags in this checkout (fetch them with 'git fetch --tags'); tag checks skipped"]


def test_a_directory_that_is_not_a_git_checkout_skips_tag_checks(tmp_path: Path) -> None:
    report = check.run(_repository(tmp_path / "export"))

    assert report.errors == []
    assert report.notes == ["not a git checkout (or git is unavailable); tag checks skipped"]


def test_a_mismatched_changelog_version_fails(tmp_path: Path) -> None:
    _repository(tmp_path, "0.3.0", **{"CHANGELOG.md": CHANGELOG.replace("{version}", "0.2.9")})

    errors = check.run(tmp_path).errors

    assert errors == ["CHANGELOG.md's newest release is 0.2.9, but pyproject.toml says 0.3.0"]


def test_an_unreleased_changelog_section_and_badly_formed_headings(tmp_path: Path) -> None:
    unreleased = "# Changelog\n\n## Unreleased\n\n- Next.\n\n" + CHANGELOG.split("\n\n", 1)[1]
    _repository(tmp_path, **{"CHANGELOG.md": unreleased})
    assert check.run(tmp_path).errors == []

    _repository(tmp_path, **{"CHANGELOG.md": CHANGELOG + "\n## 0.2.1 (2026-11-01)\n"})
    assert check.run(tmp_path).errors == ["CHANGELOG.md heading '## 0.2.1 (2026-11-01)' is not 'X.Y.Z — YYYY-MM-DD'"]

    _repository(tmp_path, **{"CHANGELOG.md": "# Changelog\n"})
    assert check.run(tmp_path).errors == ["CHANGELOG.md has no release heading"]


def test_historical_version_references_are_not_flagged(tmp_path: Path) -> None:
    # The fixtures mention 0.2.2, 0.2.1, v0.2.1, and "Fixed for 0.3.0" as history.
    _repository(tmp_path, "0.3.1")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG.format(version="0.3.1"), encoding="utf-8")

    assert check.run(tmp_path).errors == []


def test_a_stale_current_reference_fails_and_a_missing_one_is_reported(tmp_path: Path) -> None:
    stale = README.replace("CIRCA.git@v{version}", "CIRCA.git@v0.2.2").replace("currently {version}", "now")
    _repository(tmp_path, **{"README.md": stale})

    errors = check.run(tmp_path).errors

    assert errors == [
        "README.md: PyPI install comment reference to the current version not found",
        "README.md: GitHub tag install names 0.2.2, but pyproject.toml says 0.3.0",
    ]


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        (
            'git tag -a v0.3.1 -m "CIRCA v0.3.1"\ngit push origin v0.2.3',
            ["docs/EXTERNAL_SETUP.md:6: pushes v0.2.3 after tagging v0.3.1"],
        ),
        (
            'git tag -a v0.3.1 -m "CIRCA v0.3.2"\ngit push origin v0.3.1',
            ["docs/EXTERNAL_SETUP.md:5: tag message 'CIRCA v0.3.2' doesn't match v0.3.1"],
        ),
        (
            'git tag -a v0.2.3 -m "CIRCA v0.2.3"\ngit push origin v0.2.3',
            ["docs/EXTERNAL_SETUP.md:5: tag example v0.2.3 is older than the current version 0.3.0"],
        ),
        ('git tag -a v1.0.0 -m "CIRCA v1.0.0"\n\ngit push origin v1.0.0', []),
    ],
)
def test_git_tag_and_push_examples(tmp_path: Path, example: str, expected: list[str]) -> None:
    setup = SETUP.split("```bash\n")[0] + "```bash\n" + example + "\n```\n"
    _repository(tmp_path, **{"docs/EXTERNAL_SETUP.md": setup})

    assert check.run(tmp_path).errors == expected


def test_a_version_that_is_not_a_plain_release_fails(tmp_path: Path) -> None:
    _repository(tmp_path, "0.3.0rc1")

    assert check.run(tmp_path).errors == ["pyproject.toml version '0.3.0rc1' is not a plain X.Y.Z release"]


def _fetch(pypi: str, github: str) -> Any:
    responses = {
        "https://pypi.org/pypi/circa-ci/json": {"info": {"version": pypi}},
        "https://api.github.com/repos/scmbuildrelease/CIRCA/releases/latest": {"tag_name": github},
    }
    return responses.__getitem__


def test_online_mode_checks_the_latest_pypi_and_github_releases(tmp_path: Path) -> None:
    _repository(tmp_path)

    assert check.run(tmp_path, online=True, fetch=_fetch("0.3.0", "v0.3.0")).errors == []
    assert check.run(tmp_path, online=True, fetch=_fetch("0.2.2", "v0.2.1")).errors == [
        "PyPI's latest circa-ci release is 0.2.2, not 0.3.0",
        "GitHub's latest release is v0.2.1, not v0.3.0",
    ]


def test_online_mode_fails_when_an_index_cannot_be_reached(tmp_path: Path) -> None:
    _repository(tmp_path, **{"pyproject.toml": PYPROJECT.replace("github.com", "example.com")})

    def unreachable(url: str) -> Any:
        raise OSError("network unreachable")

    assert check.run(tmp_path, online=True, fetch=unreachable).errors == [
        "could not verify PyPI: network unreachable",
        "project.urls.Homepage is not a github.com repository URL; GitHub not verified",
    ]
    _repository(tmp_path)
    assert check.run(tmp_path, online=True, fetch=unreachable).errors[-1] == (
        "could not verify the GitHub release: network unreachable"
    )


def test_main_exit_status_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repository(tmp_path)
    assert check.main(["--root", str(tmp_path)]) == 0
    assert "Release consistency: OK for version 0.3.0 (tag v0.3.0)" in capsys.readouterr().out

    _repository(tmp_path, **{"CHANGELOG.md": CHANGELOG.replace("{version}", "0.2.9")})
    assert check.main(["--root", str(tmp_path)]) == 1
    assert "1 problem(s) for version 0.3.0" in capsys.readouterr().err


def test_this_repository_is_consistent() -> None:
    assert check.run(ROOT).errors == []
