# Changelog

All notable changes to CIRCA are listed here. Versions follow
[Semantic Versioning](https://semver.org); while CIRCA is below 1.0, a minor
version may change behavior.

## 0.3.0 — 2026-11-03

### Changed

- A team incident that records evidence or a root cause now informs a
  failure's category, root cause, and recommendation only when all of its
  recorded evidence appears in the log, including at least one distinctive line:
  a tool's failure summary or generic CI words alone don't count. Shared keywords alone no longer carry a team incident's root
  cause to an unrelated failure.

### Fixed

- A packaged synthetic incident no longer decides the category or supplies the
  root cause when the log shares only ordinary words from its description. At
  least one of the incident's declared keywords must appear. Two real logs that
  were wrongly called `hardware` (a missing API key) and `infrastructure` (a
  PyTorch `RuntimeError`) are now `unknown`. Results on the synthetic `evaluate`
  datasets and `benchmark` are unchanged.
- When the local LLM is not used, `investigate-log` now says why: no Ollama
  server is reachable (the URL is shown without credentials), the model is not
  installed, the request timed out, or the reply was invalid. `LLM called` is
  now `no` when the server was unreachable or the model was not installed,
  because no model ran.

### Documentation

- README installs `circa-ci` from PyPI, and its version and wheel name now
  match the release (the README for 0.2.2 still showed 0.2.1).
- Added `docs/INTERVIEW_DEMO.md`: three reproducible demos (known, unknown, and
  insufficient-evidence failures) that use temporary knowledge files.

## 0.2.2 — 2026-11-01

Packaging only; analysis behavior is unchanged.

- Prepared for PyPI as `circa-ci`, because the name `circa` belongs to an
  unrelated project. The command (`circa`) and the import name (`circa`) are
  unchanged. If an earlier version is installed, run `pip uninstall circa`
  before installing 0.2.2; removing it afterwards also removes the `circa`
  command.
- Added a manually run publishing workflow that uses PyPI trusted publishing
  (no stored API token). It publishes only a tag that matches the package
  version, to TestPyPI or, through the `pypi` environment, to PyPI.
- The source distribution no longer includes the team knowledge file, tests,
  evaluation dataset, agent instructions, or CI configuration. The build
  backend is pinned.
- README links to CONTRIBUTING, SECURITY, and CHANGELOG are absolute, so they
  work on the PyPI project page.

## 0.2.1 — 2026-11-01

### Security

- Fixed partial redaction of URL passwords that contain `@`. In v0.2.0,
  `https://deploy:pass@word@host/` became `https://deploy:[REDACTED]@word@host/`,
  leaving the part of the password after its first `@` in analysis,
  fingerprints, LLM prompts, terminal and JSON output, and `pending_review`
  drafts that `investigate-log` saved to a knowledge file. The whole password is
  now redacted, up to the last `@` before the host. URL passwords without `@`
  were fully redacted in v0.2.0 too.
- Output already produced by v0.2.0 is not repaired automatically. If a CI log
  you processed with v0.2.0 had a URL password containing `@`, treat that
  password as exposed and rotate it. To find saved drafts with a leftover, run
  `grep -cE '\[REDACTED\]@[^ /?#"]*@' path/to/incidents.json` (any count above
  0), then edit those values to `[REDACTED]` as described in
  [SECURITY.md](SECURITY.md#repairing-a-knowledge-file). Saved reports and
  copied terminal output need the same review.
- Dedup fingerprints change only for logs that contain such URLs.

## 0.2.0 — 2026-11-01

Changes since v0.1.0 (2026-10-22). CIRCA is still local-first: analysis is
deterministic and works without any AI service, and the only runtime Python
dependencies are Pydantic and Typer.

### Investigation workflow

- New `circa investigate-log` command. It runs the full workflow on one log:
  deterministic analysis, then verified team incidents (a strong match with a
  validated fix is returned as a known solution with no LLM call), then
  already-recorded failures, and only then at most one call to an optional
  local Ollama model.
- A useful LLM diagnosis is saved as a `pending_review` draft. A draft needs a
  category, a root cause grounded in the log, and at least one quoted log line.
  Suggestions that would weaken a safeguard (disable, skip, bypass, `--force`,
  thresholds, baselines) are left out and named in a note.
- The deterministic category stays authoritative; the LLM category is used
  only when the analyzer found none, and `draft_origin.category_source` records
  where it came from.

### GitHub Actions

- New `circa runs` command lists a repository's recent failed runs through the
  GitHub CLI (`gh`).
- `investigate-log` accepts a run URL, a run ID (with `--repo` or a detected
  repository), or no source, in which case it lists failed runs and asks you to
  choose one.
- The repository is detected from the checkout's github.com remotes. CIRCA
  stops instead of guessing when remotes disagree, and never prints remote
  URLs, which can contain credentials. CIRCA never reads the `gh` token.

### Team knowledge and human review

- Team incident knowledge lives in a JSON file passed explicitly with
  `--knowledge`, for example `team/incidents.json`.
- New `circa knowledge` commands: `add`, `update`, `verify`, `reject`,
  `supersede`, `show`, `list`, and `validate`.
- Lifecycle: `pending_review` → `verified` → `superseded`, or `rejected`. Only
  `verified` incidents are matched. Every write shows the record, asks for
  confirmation (or `--yes`), writes atomically, and refuses if the file changed
  since it was read.
- A real incident can be verified only with a root cause, a reviewer, and a fix
  validated by a named method with evidence. LLM output and a single
  successful retry never validate a fix.
- Exact duplicates (same dedup fingerprint) are rejected; near duplicates need
  `--allow-similar`.

### Setup and first-time use

- New `circa doctor` (read-only prerequisite checks) and `circa init` (creates
  an empty knowledge file only if it does not exist).
- New `circa --version`.
- Rewritten README with a quick start, tutorial, and troubleshooting table.

### Deterministic analysis

- Performance-threshold assessment for logs with an explicit threshold
  violation. It reports a candidate cause as a hypothesis, never changes
  thresholds, and does not change the category.
- Errors are matched line by line in pipeline-stage order, so the earliest
  failing stage wins. Informational lines and printed source code (diffs,
  Bandit snippets) are not treated as errors.
- A historical root cause is reused only when its pipeline stage fits the
  failure.
- Newly recognized failures: `npm ERR!`, Ruff lint and format, Prettier, mypy,
  Bandit findings, C/C++ compiler errors, and Python `SyntaxError`,
  `IndentationError`, and `TabError`. File, line, and rule details are reported
  when the log states them.

### Evaluation

- New `circa evaluate` measures deterministic classification and root-cause
  correctness on labeled cases (`evaluation/cases.json`, including sanitized
  real CI logs) plus the 35 legacy benchmark cases. It is read-only and never
  reads team knowledge.
- On the shipped dataset, category accuracy is 48/60 confirmed cases with no
  false positives; the legacy set stays at 27/35. These numbers describe
  CIRCA's own labeled cases, not production accuracy.
- The retrieval benchmark (`circa benchmark`) and its synthetic dataset are
  unchanged.

### Security

- Credentials are redacted before any processing. Recognized values become
  `[REDACTED]` while the rest of the message is kept. This covers GitHub,
  GitLab, Slack, npm, PyPI, Google, and OpenAI/Anthropic-style tokens; AWS
  access key IDs; JSON Web Tokens; `Authorization` and bearer values; passwords
  in URLs; credential options and assignments (such as `NPM_TOKEN=...`); and PEM
  private keys.
- Fingerprints, analysis, LLM prompts, drafts, and terminal and JSON output all
  use the redacted text. Fingerprints of logs without credentials are
  unchanged.
- Redaction fails closed: if it cannot complete, the command stops with a safe
  error before anything reads the log.
- Credentials in LLM output are redacted and block the draft. Credentials in a
  draft's source name or model setting are redacted.
- Knowledge records containing a recognized credential are rejected in every
  text field, including `draft_origin.cited_log_lines`. The error names the
  field (for example `evidence[1]`) but never the value.
- Terminal control sequences in CI logs and GitHub output are removed before
  display. Invalid `circa analyze` input reports field errors without quoting
  values, and tracebacks never show local variables.
- CI quality gate: Prettier, Ruff, tests on Python 3.12 and 3.13 with a 90%
  coverage floor, mypy, Bandit, pip-audit, Gitleaks, actionlint, zizmor, and a
  wheel install smoke test. Actions are pinned to commit SHAs with read-only
  permissions.

### Upgrade notes

- **Knowledge files may need a one-time repair.** Credential detection is
  broader than in v0.1.0, so a team file that loaded before can now fail with
  `incident text matches a secret or credential pattern in <field>`. Replace
  the named value with `[REDACTED]` and run `circa knowledge validate`. See
  [SECURITY.md](SECURITY.md#repairing-a-knowledge-file). Records are never
  changed or deleted automatically.
- Lines containing credentials are now kept in redacted form instead of being
  dropped from investigation evidence. Dedup fingerprints change only for such
  logs.
- The redaction placeholder is `[REDACTED]`. Text already masked as `***` or
  `<redacted>` is still accepted.

### Known limitations

- Classification is rule- and keyword-based. Failures outside the recognized
  signatures are reported as `unknown`, and only one explicit failure kind is
  named per log.
- Credential detection is pattern-based, not complete (see
  [SECURITY.md](SECURITY.md)).
- GitHub support is limited to github.com through the GitHub CLI. GitHub
  Enterprise and other CI systems are not supported.
- The LLM path needs a local Ollama server; small local models give variable
  hypotheses, which is why every draft requires human review.
- `circa evaluate` needs a repository checkout; its dataset is not in the
  wheel.

## 0.1.0 — 2026-10-22

First release: deterministic failure analysis of structured failures
(`circa analyze`) and raw logs (`circa analyze-log`), packaged synthetic
historical incidents, keyword retrieval with optional local semantic retrieval,
optional local LLM explanation and classification, and the retrieval benchmark
(`circa benchmark`).
