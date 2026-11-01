# Contributing to CIRCA

Thank you for helping improve CIRCA. This guide covers setting up a
development environment, running the checks CI runs, and what a pull request
needs.

## Development setup

Requirements: Python 3.12 or 3.13, and Git. Node.js (for Prettier) and the
standalone `actionlint` and `gitleaks` tools are needed only to reproduce every
CI check locally.

```bash
git clone https://github.com/scmbuildrelease/CIRCA.git && cd CIRCA
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
circa --version
```

The `dev` extra installs pytest, pytest-cov, and pinned versions of Ruff and
mypy. Ollama and the GitHub CLI are not needed for development: tests use fakes
and never call a model, GitHub, or the network.

## Tests and quality checks

Run these before opening a pull request. They match the CI jobs in
`.github/workflows/ci.yml`.

```bash
python -m pytest --cov --cov-report=term-missing   # fails below 90% coverage
ruff check .
ruff format --check .
mypy
npx --yes prettier@3.9.9 --check "**/*.{md,yml,yaml,json}"
pipx run bandit==1.9.4 -c pyproject.toml -r src
pipx run pip-audit==2.10.1 --strict .
pipx run zizmor==1.30.1 .github/workflows
actionlint
gitleaks git --redact .
python -m pip install build==1.6.1 && python -m build   # sdist and wheel in dist/
```

CI runs the tests on Python 3.12 and 3.13. CI only reports problems; it never
formats or fixes files. Run `ruff format .` and
`npx prettier@3.9.9 --write <files>` yourself.

Two read-only regression checks are useful when you change analysis code.
Report any change in their output in your pull request:

```bash
circa evaluate     # deterministic classification on evaluation/cases.json
circa benchmark    # retrieval on the packaged synthetic dataset
```

## Project principles

These come from [AGENTS.md](AGENTS.md); please follow them in every change.

- **Deterministic analysis is the primary authority.** It must work without any
  LLM. The LLM is optional, local (Ollama), and never replaces or overrides a
  deterministic result.
- **Keep dependencies minimal.** The runtime depends only on Pydantic and
  Typer. Do not add a dependency, external service, or large framework without
  discussing it in an issue first.
- **Keep changes small and focused.** Reuse existing code before adding
  abstractions, and change only what the pull request is about.
- **Add tests for new behavior,** including negative cases (what must _not_ be
  recognized), and run the full suite.
- **Do not tune rules to evaluation labels.** Add evaluation cases when a real
  failure is understood, not to make a metric go up.

## Data and security rules

- **Never commit real credentials, private CI logs, or customer data.** Test
  fixtures use synthetic values. Assemble credential-shaped test strings at
  runtime (for example `"ghp" + "_" + ...`) so Gitleaks does not flag them.
- **Packaged incidents and evaluation data are read-only and synthetic.** Real
  logs under `evaluation/logs/` must be sanitized.
- **Do not edit `team/incidents.json` by hand in a feature pull request.**
  Changes to team knowledge go through the `circa knowledge` commands and human
  review.
- **Never mark a fix `validated` without real validation evidence.** LLM output
  and a single successful retry are not validation.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Pull requests

- Open an issue first for anything beyond a small fix, so the approach can be
  agreed on.
- Describe what changed and why, and how you tested it. Include `circa evaluate`
  or `circa benchmark` differences if analysis behavior changed.
- Keep CLI and JSON output backward compatible unless the change is the point of
  the pull request; call out any change explicitly.
- Update `README.md` and `CHANGELOG.md` (under the unreleased version) when user
  visible behavior changes.
- All CI checks must pass before merging.

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
