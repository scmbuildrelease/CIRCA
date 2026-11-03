# External Setup

This document lists the accounts, services and repository settings outside the
source tree that a maintainer needs to develop and publish CIRCA. It describes
the 0.3.0 release (2026-11-03). Observations about accounts and environments
were made on 2026-11-02 unless stated otherwise.

It does not repeat the README. Using CIRCA, installing it, setting up the GitHub
CLI and Ollama, running the CI checks locally and enforcing them before merge
are covered there.

## 1. Overview

| Area                          | What is external                               | Needed for                   |
| ----------------------------- | ---------------------------------------------- | ---------------------------- |
| GitHub repository             | `scmbuildrelease/CIRCA`, Actions, environments | CI, releases                 |
| PyPI (`pypi.org`)             | project `circa-ci`, Trusted Publisher          | production releases          |
| TestPyPI (`test.pypi.org`)    | project `circa-ci`, Trusted Publisher          | trial releases               |
| GitHub CLI login              | the maintainer's own `gh` session              | `circa runs`, real-log cases |
| Ollama (local, not a service) | a local server and model on your machine       | optional LLM drafts          |

CIRCA uses **no cloud AI service** and stores **no secrets** in GitHub. CI needs
no credentials. Publishing authenticates with short-lived OpenID Connect (OIDC)
tokens instead of PyPI API tokens.

## 2. Existing documentation references

| Topic                                                 | Where                                                                           |
| ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| Install, requirements, `gh auth login`                | README §4–5                                                                     |
| Ollama install, `CIRCA_LLM_MODEL`, `CIRCA_OLLAMA_URL` | README §10                                                                      |
| CI checks, local reproduction, branch rulesets        | README "CI quality gate"                                                        |
| Contribution rules                                    | `CONTRIBUTING.md`                                                               |
| Vulnerability reporting, knowledge-file handling      | `SECURITY.md`                                                                   |
| Release notes                                         | `CHANGELOG.md`                                                                  |
| Real-log evaluation and preserved evidence            | `docs/phase-23-real-world-dataset.md`, `docs/phase-24-evidence-preservation.md` |

## 3. External accounts

A maintainer needs:

- **A GitHub account** with admin rights on `scmbuildrelease/CIRCA`. Admin is
  needed to manage environments, rulesets and Actions settings. Write access is
  enough to push tags and run the Publish workflow.
- **A PyPI account and a separate TestPyPI account.** They are independent
  services with separate logins, 2FA and project ownership.
- **Optional:** a local Ollama install. It has no account.

Use two-factor authentication on all three. Never share an account; add each
maintainer as their own collaborator or project member.

## 4. PyPI and TestPyPI

Do these steps on **both** https://pypi.org/ and https://test.pypi.org/.

1. **Register** an account and **verify the email address**. PyPI does not let an
   unverified account upload or manage projects.
2. **Enable two-factor authentication** (an authenticator app or a security
   key). PyPI requires 2FA on every account. Store the recovery codes offline,
   never in the repository, an issue or CI.
3. **Project ownership.** The distribution name is `circa-ci`; `circa` on PyPI
   is an unrelated project. Release 0.2.2 already exists on both indexes, so
   the project exists. Under **Your projects → circa-ci → Collaborators**, check
   that at least two trusted maintainers are **Owners**, so the project can't
   be lost with one account.
4. **Trusted Publisher.** See [section 7](#7-oidc-trusted-publishing).

You never need a PyPI API token for CIRCA. If one exists from earlier testing,
delete it under **Account settings → API tokens**.

## 5. GitHub repository configuration

What the source configures, and what can only be set in **Settings**:

- **Actions permissions.** Both workflows start with `permissions: {}` and grant
  each job only what it needs (`contents: read`; `id-token: write` only in the
  two publish jobs). No repository setting is required for this. Keeping
  **Settings → Actions → General → Workflow permissions** at "Read repository
  contents" is still recommended.
- **Secrets.** None are used. The repository and both environments have
  0 secrets and 0 variables. `GH_TOKEN` in the zizmor step is the built-in
  read-only job token.
- **Branch protection for `main`.** Not configured: there is no ruleset and no
  branch protection. README "Enforcing the gate before merge" explains how to
  add it.
- **Tag protection.** Not configured. Because the environments deploy from any
  `v*` tag (section 6), anyone who can push a `v*` tag can start a release.
  Consider a tag ruleset (**Settings → Rules → Rulesets → New tag ruleset**,
  target `v*`) that restricts creation, update and deletion to maintainers.

## 6. GitHub Environments

`publish.yml` uses two environments:

| Environment | Job                 | Purpose                            | Environment URL                  |
| ----------- | ------------------- | ---------------------------------- | -------------------------------- |
| `testpypi`  | Publish to TestPyPI | trial upload to verify the package | https://test.pypi.org/p/circa-ci |
| `pypi`      | Publish to PyPI     | production upload                  | https://pypi.org/p/circa-ci      |

The URL is shown on the run's summary page and in the repository's
**Deployments** list. It is set by the workflow, not in Settings.

**What the workflow can't guarantee.** Naming an environment in a workflow does
not configure it. If an environment doesn't exist, GitHub creates it with no
protection on first use. Its rules live only in **Settings → Environments**.

Current state, read on 2026-11-02 with `gh api repos/scmbuildrelease/CIRCA/environments`:

| Setting                      | `testpypi`       | `pypi`           |
| ---------------------------- | ---------------- | ---------------- |
| Exists                       | yes              | yes              |
| Deployment branches and tags | tag pattern `v*` | tag pattern `v*` |
| Required reviewers           | none             | **none**         |
| Wait timer                   | none             | none             |
| Admins can bypass            | yes              | yes              |
| Secrets / variables          | 0 / 0            | 0 / 0            |

The `publish.yml` header says "The `pypi` environment should require a
reviewer's approval". **That is not configured.** To add it (repository admin):

1. **Settings → Environments → pypi.**
2. Enable **Required reviewers** and add one or more maintainers. Consider
   enabling **Prevent self-review**, so the person who started the run can't
   approve it.
3. Optionally clear **Allow administrators to bypass configured protection
   rules**.
4. Leave **Deployment branches and tags** as **Selected branches and tags** with
   the tag rule `v*`.

`testpypi` doesn't need a reviewer. Keep its `v*` tag rule.

## 7. OIDC Trusted Publishing

**Implemented in source** (`.github/workflows/publish.yml`):

| Item            | Value                                                         |
| --------------- | ------------------------------------------------------------- |
| Owner           | `scmbuildrelease`                                             |
| Repository      | `CIRCA`                                                       |
| Workflow file   | `publish.yml` (`.github/workflows/publish.yml`)               |
| Environments    | `testpypi` (TestPyPI job), `pypi` (PyPI job)                  |
| Authentication  | GitHub Actions OIDC; no stored token                          |
| Job permission  | `id-token: write`, only in the two publish jobs               |
| Publisher       | `pypa/gh-action-pypi-publish` v1.14.3, pinned to a commit SHA |
| TestPyPI upload | `repository-url: https://test.pypi.org/legacy/`               |

The action exchanges the job's OIDC token for a short-lived upload token. PyPI
accepts it only if the token's claims match a Trusted Publisher registered for
the project.

**Configure on each index** (project **circa-ci → Manage → Publishing → Add a
new publisher → GitHub**):

| Field            | PyPI              | TestPyPI          |
| ---------------- | ----------------- | ----------------- |
| Owner            | `scmbuildrelease` | `scmbuildrelease` |
| Repository name  | `CIRCA`           | `CIRCA`           |
| Workflow name    | `publish.yml`     | `publish.yml`     |
| Environment name | `pypi`            | `testpypi`        |

The workflow name is the file name only, not the path. Setting the environment
name means only that environment's job can upload, and so only with that
environment's protection rules. Before a project exists, the same form is under
**Account → Publishing** as a "pending publisher".

**Status: external configuration needs verification.**

- Some publisher is configured on each index. 0.2.2 was uploaded to TestPyPI by
  run 41348857022 and to PyPI by run 41349555807, and each index's provenance
  for the files names `scmbuildrelease/CIRCA` and `publish.yml`.
- That provenance shows **no environment** (`environment: null`). This suggests
  the publishers were registered without an environment name. If so, any job in
  `publish.yml` could upload to either index, regardless of the `pypi`
  environment's rules. A project owner should open **Manage → Publishing** on
  each index and add the environment names from the table above.
- Trusted Publisher settings can't be read through a public API, so this can
  only be checked by a project owner, in the browser.

## 8. Release and publishing procedure

`publish.yml` runs only by hand (`workflow_dispatch`) and has one input,
`index`: `testpypi` (default) or `pypi`. It has no secrets and no other inputs.

**What the workflow checks.** The build job reads `project.version` from
`pyproject.toml` and stops unless the run was started **on a tag** named exactly
`v<version>`. Starting it on a branch, including `main`, or on any other tag
fails with:

```text
Run this workflow on tag v<version>, not <ref type> <ref name>.
```

It then builds the sdist and wheel, runs `twine check --strict`, installs the
wheel in a clean virtual environment, and runs `circa --version` and
`circa analyze-log` against `examples/demo_disk_full.log`. It does **not** run
the CI quality gates; those come from `ci.yml` on the tagged commit.

**Steps:**

1. **Version.** In a pull request, set `project.version` in `pyproject.toml`.
2. **Docs.** Add the release to `CHANGELOG.md`, and update the README's install
   examples to the new version (see section 12 for the current gap).
3. **CI.** Merge only when every `ci.yml` check passes on `main`.
4. **Tag.** Tag the merged commit and push the tag:
   ```bash
   git tag -a v0.3.1 -m "CIRCA v0.3.1"   # must equal "v" + project.version
   git push origin v0.2.3
   ```
   PyPI never accepts the same version twice, so a mistake needs a new version.
5. **Actions → Publish → Run workflow.**
6. Under **Use workflow from**, choose **Tags → v0.3.1**, not a branch.
7. Choose **index: testpypi** and run it.
8. **Verify** the TestPyPI release in a fresh virtual environment:
   ```bash
   python -m pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ "circa-ci==0.3.1"
   circa --version
   ```
   The extra index supplies Pydantic and Typer, which TestPyPI may not have.
9. Run the workflow again on the same tag with **index: pypi**.
10. Once required reviewers are configured, approve the `pypi` deployment from
    the run page (**Review deployments**). Until then, the job starts without
    approval.

Each run builds again from the tag. The build backend is pinned, but the PyPI
files are not guaranteed to be byte-identical to the TestPyPI ones.

## 9. CI/CD infrastructure

| Workflow      | Trigger                                  | Purpose                                                                                 | Credentials             |
| ------------- | ---------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------- |
| `ci.yml`      | every branch push, pull requests, manual | quality gates: format, lint, tests, types, security, workflow audit, package smoke test | none (read-only token)  |
| `publish.yml` | manual only, on a `v*` tag               | build, check and upload a release                                                       | OIDC, `id-token: write` |

The individual CI checks are listed in README "CI quality gate". CI only
reports; it never publishes, tags or commits. Publishing never runs on its own.

All jobs run on GitHub-hosted `ubuntu-latest` runners. External downloads at
run time: actions pinned by commit SHA, PyPI packages pinned by version,
`prettier@3.9.9` from npm, and the Gitleaks and actionlint release archives,
whose SHA-256 checksums are verified. There are no self-hosted runners.

## 10. Troubleshooting

| Symptom                                                                        | Cause and fix                                                                                    |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `Run this workflow on tag vX.Y.Z, not branch main`                             | Started on a branch. Rerun with **Use workflow from → Tags**                                     |
| `... not tag vA.B.C` with a tag selected                                       | The tag doesn't match `project.version` at that commit. Tag the commit that has the version bump |
| `Tag "…" is not allowed to deploy to pypi due to environment protection rules` | The ref doesn't match the environment's `v*` tag rule                                            |
| `invalid-publisher` / `Trusted publishing exchange failure`                    | The index has no publisher matching owner, repository, `publish.yml` and environment. Check §7   |
| `File already exists`                                                          | That version is already on the index. Bump the version and tag again                             |
| TestPyPI install can't find `pydantic` or `typer`                              | Add `--extra-index-url https://pypi.org/simple/` (§8, step 8)                                    |
| `circa doctor`: model `qwen2.5:1.5b` not installed                             | `ollama pull qwen2.5:1.5b`, or unset `CIRCA_LLM_MODEL` to use the default `llama3.2`             |
| `Local LLM unavailable` with Ollama running                                    | `CIRCA_OLLAMA_URL` points elsewhere. The default is `http://localhost:11434`                     |

### Local model notes

The README covers installing Ollama. For maintainers:

- **Local only.** CIRCA calls the Ollama HTTP API on your machine
  (`GET /api/tags` for `doctor`; `POST /api/generate` for diagnoses;
  `POST /api/embed` for semantic retrieval). It never calls a cloud AI service.
  If you set `CIRCA_OLLAMA_URL` to another host, the extracted failure lines are
  sent there, so use only hosts you control.
- **Default model:** `llama3.2` (`src/circa/llm.py`). **Qwen 2.5 1.5B** is a
  smaller alternative that the README shows:
  `ollama pull qwen2.5:1.5b`, then `export CIRCA_LLM_MODEL=qwen2.5:1.5b`.
- **Embeddings** for `circa benchmark` and `analyze --semantic-retrieve` use
  `nomic-embed-text` (`CIRCA_EMBEDDING_MODEL`). Without it, semantic metrics are
  reported as unavailable.
- **CI and tests need no model.** The test suite mocks Ollama.

## 11. Security and credential management

- **No long-lived publishing credential exists.** Don't add a PyPI token as a
  repository or environment secret; it would bypass the Trusted Publisher and
  environment controls.
- **Account security.** Use 2FA on GitHub, PyPI and TestPyPI, and keep recovery
  codes offline. Remove collaborators and project owners who no longer
  maintain CIRCA.
- **`gh` login.** CIRCA uses the maintainer's existing GitHub CLI session and
  never reads or prints its token (README §5). It needs only read access to
  Actions logs.
- **Real CI logs.** Preserve only sanitized excerpts under `evaluation/logs/`
  with `evaluation/preserve.py`, and review each one for credentials, internal
  hostnames, private paths, usernames, emails and infrastructure identifiers
  before committing (Phase 24). Raw logs never go into `team/incidents.json`.
- **Gitleaks** scans the full history in CI. If it finds a real secret, rotate
  the secret first; removing it from history is not enough.
- **Vulnerabilities:** report privately as described in `SECURITY.md`.

## 12. Configuration status checklist

Labels: **CONFIGURED IN SOURCE**, **EXTERNAL CONFIGURATION NEEDS VERIFICATION**,
**NOT CONFIGURED**, **OPTIONAL**. Observations are from 2026-11-02.

| Item                                                      | Status                                    | Evidence or action                                            |
| --------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------- |
| CI quality gates (`ci.yml`)                               | CONFIGURED IN SOURCE                      | 10 checks; minimal permissions; SHA-pinned actions            |
| Publish workflow, tag/version check, build and smoke test | CONFIGURED IN SOURCE                      | `publish.yml` build job                                       |
| OIDC (`id-token: write`, `gh-action-pypi-publish`)        | CONFIGURED IN SOURCE                      | publish jobs only                                             |
| Environment `testpypi` (exists, `v*` tags)                | EXTERNAL CONFIGURATION NEEDS VERIFICATION | seen through the API; recheck in Settings before each release |
| Environment `pypi` (exists, `v*` tags)                    | EXTERNAL CONFIGURATION NEEDS VERIFICATION | seen through the API; recheck in Settings                     |
| Required reviewer on `pypi`                               | NOT CONFIGURED                            | add it (§6); the workflow comment expects it                  |
| PyPI Trusted Publisher                                    | EXTERNAL CONFIGURATION NEEDS VERIFICATION | worked for 0.2.2; provenance shows no environment name (§7)   |
| TestPyPI Trusted Publisher                                | EXTERNAL CONFIGURATION NEEDS VERIFICATION | same as PyPI                                                  |
| PyPI and TestPyPI project ownership and 2FA               | EXTERNAL CONFIGURATION NEEDS VERIFICATION | `circa-ci` 0.2.2 exists on both; check owners (§4)            |
| Branch protection or ruleset for `main`                   | NOT CONFIGURED                            | README "Enforcing the gate before merge"                      |
| Tag ruleset for `v*`                                      | NOT CONFIGURED                            | recommended (§5)                                              |
| Repository and environment secrets                        | CONFIGURED IN SOURCE (none needed)        | 0 secrets, 0 variables                                        |
| Ollama and a local model                                  | OPTIONAL                                  | only for LLM drafts and semantic retrieval                    |
| GitHub CLI login                                          | OPTIONAL                                  | only for GitHub run fetching                                  |

### Version consistency (updated 2026-11-03)

For release 0.3.0, `pyproject.toml`, the README's install examples and
`CHANGELOG.md` all say 0.3.0, and the tag is `v0.3.0`. Check the published
side after each release:

```bash
git ls-remote --tags origin v0.3.0                 # tag exists on GitHub
gh release view v0.3.0 --repo scmbuildrelease/CIRCA
python -m pip index versions circa-ci              # PyPI lists 0.3.0
```

Earlier discrepancies, kept for the record:

1. The README for 0.2.2 still installed `v0.2.1` and named the wheel
   `circa-0.2.1-py3-none-any.whl`; it didn't mention PyPI. Fixed for 0.3.0.
2. `v0.2.2` was tagged and published to PyPI, but no GitHub Release was
   created for it, so GitHub's "Latest" release stayed at v0.2.1.
3. **Minor:** `v0.1.0` is a lightweight tag; later tags are annotated.
