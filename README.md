# CIRCA

**Continuous Integration Root Cause Analyzer**

CIRCA is a local tool for analyzing CI failures and identifying whether the likely cause is software, hardware, network, infrastructure, or unknown.

## v0.1 scope

The first version will use deterministic failure fingerprints and historical incident matching to produce a classification, root cause, and recommendation.

```text
CI Failure → Fingerprint → Historical Knowledge → Classification → Root Cause → Recommendation
```

Future versions may add Jenkins, Perforce, AI agents, Slack, and infrastructure integrations. These are not part of v0.1.

## Development

Requires Python 3.12+.

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
```

## CI quality gate

`.github/workflows/ci.yml` runs on every branch push, on pull requests (opened,
reopened, or synchronized), and on demand through `workflow_dispatch`. All checks
run on GitHub; no local pre-commit hooks are required. CI only reports problems
and never formats, fixes, or commits files. Ordinary checks do not need Ollama,
embedding models, external LLM APIs, credentials, or private infrastructure.

| Check name                             | What it enforces                                                                 |
| -------------------------------------- | -------------------------------------------------------------------------------- |
| `Format (Prettier)`                    | Markdown, YAML, and JSON formatting (Prettier 3.9.9, check only)                 |
| `Lint (Ruff)`                          | `ruff check` and `ruff format --check` (Ruff 0.16.10, `pyproject.toml` settings) |
| `Tests (Python 3.12)`                  | Full pytest suite with line and branch coverage; fails below 90%                 |
| `Tests (Python 3.13)`                  | Same suite on the newest supported interpreter                                   |
| `Type check (mypy)`                    | mypy 2.4.0 with the Pydantic plugin over `src/`                                  |
| `Security (Bandit)`                    | Python security lint of `src/`                                                   |
| `Security (pip-audit)`                 | Known vulnerabilities in the declared runtime dependencies                       |
| `Security (Gitleaks)`                  | Secrets anywhere in git history; findings are redacted in logs                   |
| `Workflows (actionlint + zizmor)`      | Workflow validity, shell scripts in `run:` steps, and Actions security issues    |
| `Package (build + install smoke test)` | Builds sdist and wheel, installs the wheel in a clean venv, runs the CLI         |

Third-party actions are pinned to full commit SHAs, downloaded binaries are
checksum-verified, every job has only `contents: read`, and checkouts do not
persist credentials. Pushes to a pull request's branch run both the `push` and
`pull_request` events; this is intentional so that every branch is checked
even without a pull request.

### Reproducing CI checks locally

After `python -m pip install -e ".[dev]"`:

```bash
npx --yes prettier@3.9.9 --check "**/*.{md,yml,yaml,json}"
ruff check .
ruff format --check .
python -m pytest --cov --cov-report=term-missing
mypy
pipx run bandit==1.9.4 -c pyproject.toml -r src
pipx run pip-audit==2.10.1 --strict .
python -m pip install build==1.6.1 && python -m build
```

Workflow and secret checks need the standalone tools: `actionlint`,
`pipx run zizmor==1.30.1 .github/workflows`, and `gitleaks git --redact .`.

### Enforcing the gate before merge

GitHub Actions reports status but does not block a merge by itself. To block
merging into `main`, open **Settings → Rules → Rulesets → New branch ruleset**
(or **Settings → Branches → Add branch protection rule**), target the default
branch, and enable:

- **Require a pull request before merging**.
- **Require status checks to pass**, adding each check name in the table
  above. Run the workflow once first so GitHub can suggest the names.
- **Require branches to be up to date before merging** (recommended).
- **Block force pushes** and restrict deletions.

CI cannot stop a push from reaching GitHub; required checks stop a failing
change from being merged.

## End-to-end demo

From the repository root, install the local development package once:

```bash
python -m pip install -e ".[dev]"
```

The following demo parses a sanitized raw build log, fingerprints the extracted
failure, searches the bundled synthetic incident history, and prints the
deterministic analysis. It does not require Ollama, network access, credentials,
or any external service.

```bash
circa analyze-log examples/demo_disk_full.log
```

Verified output:

```text
CIRCA Analysis Report
Category: hardware
Confidence: 0.95
Root cause: Build failed because the worker disk had no space left for compiler output.
Evidence:
- Historical incident synthetic-hardware-disk-001 shares 7 keyword(s) and 0 environment value(s).
- Historical incident synthetic-hardware-memory-002 shares 2 keyword(s) and 0 environment value(s).
- Failure text contains filesystem.
Historical matches:
- synthetic-hardware-disk-001
- synthetic-hardware-memory-002
Recommendation: Free disk space and investigate the worker's cleanup policy.
Owner: hardware-ops
Fingerprint: 8d765a3ffda765b075f77658080166de73d6c627774e276bf5d8ae38b6a0bacd
```

The second sanitized log intentionally lacks a recognizable failure signature.
It demonstrates that CIRCA does not overstate confidence when the available
evidence cannot support a historical match or deterministic classification.

```bash
circa analyze-log examples/demo_insufficient_evidence.log
```

Verified output:

```text
CIRCA Analysis Report
Category: unknown
Confidence: 0.00
Root cause: Insufficient deterministic evidence to identify a root cause.
Evidence:
- No strong deterministic evidence.
Historical matches:
- None
Recommendation: Collect additional failure output and environment details, then retry analysis.
Owner: unknown
Fingerprint: 0c1462cd6701fa8cc21e731ad46f0c28c1bfc85d917ea5c467266a8339bf5ad1
```

These examples and their historical incidents are synthetic. CIRCA's
deterministic results are limited by the available log evidence and bundled
incident data; they are not a claim of production incident-diagnosis accuracy.
Optional local Ollama explanation, classification, and semantic retrieval are
not used by this demo.

## Performance-threshold failures

`analyze-log` also attaches a separate, deterministic performance assessment
when a log contains an explicit threshold violation (for example `PERFORMANCE
THRESHOLD VIOLATION`, or a latency/throughput line that says it exceeds or
violates a limit). A generic `AssertionError` is not treated as a performance
failure.

```bash
circa analyze-log examples/demo_performance_threshold.log
```

The assessment keeps three concepts separate: the failure type
(`performance_threshold_violation`), a candidate cause that is only a hypothesis
(`hardware_firmware_performance_drift` when firmware, driver, BIOS, or hardware
revision details appear; otherwise `possible_genuine_performance_regression` or
`undetermined`), and a recommended action (`performance_threshold_recalibration`).
Metric values, expected ranges, tolerances, units, and test names are reported
only when the log states them; anything absent is listed as missing evidence.
CIRCA never changes thresholds or baselines. Recalibration requires human
validation and approval. The assessment does not change the deterministic
category or confidence. The example log is synthetic.

## Investigating a CI log

`circa investigate-log` runs the whole workflow on one raw log:

1. The existing parser extracts failure lines; deterministic analysis runs as
   in `analyze-log`.
2. Verified historical incidents are checked first. If one has a validated fix
   and matches strongly, its root cause, fix, and validation evidence are
   returned and the LLM is **not** called. A strong match is either the same
   dedup fingerprint, or a keyword score of at least 4 that agrees with the
   deterministic category **and** whose recorded evidence lines all appear in
   this log after normalizing timestamps, IDs, and runner names. Keyword
   similarity alone is never presented as a known solution.
3. If the failure is already recorded in the team file, that record is shown,
   with no LLM call and no new draft. "Already recorded" means the same dedup
   fingerprint as any record, or a near duplicate of a `pending_review` or
   `rejected` draft. A verified record that is only similar does not stop
   diagnosis.
4. Otherwise the optional local Ollama model is asked **once** for a structured
   diagnosis hypothesis. The prompt holds only extracted failure lines (at most
   3,000 characters, credential-looking lines removed), the deterministic
   result, and up to three weak historical matches, never the raw log.
5. A useful diagnosis is saved to the `--knowledge` file as a
   `pending_review` draft through the normal lifecycle checks. Each check
   below is independent, and there is no confidence cut-off beyond the model's
   own `0`. A draft requires:
   - a category, from the analyzer or, only when it found none, from the LLM;
   - a root cause sharing at least two distinctive terms with the log (the
     analyzer's own evidence minimum);
   - at least one LLM quote that is part of an actual log line.

   A suggested fix or validation step that shares fewer than two terms with
   the log and the historical incidents the analyzer matched, or that
   would weaken a safeguard (disable, skip, bypass, ignore, unsigned,
   `--force`, thresholds, baselines), is left out of the draft and named in a
   note. Missing evidence qualifies a draft but does not block it. The draft's
   `evidence` holds only observed log lines. The LLM's output is recorded as
   `root_cause`, an unvalidated `fix`, and `draft_origin` (model, confidence,
   `category_source`, cited log lines, validation steps, missing evidence,
   related incidents).

Prerequisites: an installed `circa`. Step 4 also needs a local
[Ollama](https://ollama.com) server (`CIRCA_OLLAMA_URL`, default
`http://localhost:11434`; model `CIRCA_LLM_MODEL`, default `llama3.2`).
Without Ollama, or if the model's reply is invalid, CIRCA prints the
deterministic analysis with a warning that the cause is still unconfirmed, and
saves no draft. Without `--knowledge`, only the packaged incidents are used and
nothing is written.

Tutorial (copy and paste from the repository root; `demo-team.json` is a
throwaway file):

```bash
# 1–2. Known failure: a verified incident with a validated fix answers it, with no LLM call.
circa knowledge add examples/incident_draft.json --knowledge demo-team.json --yes
circa knowledge verify team-hardware-scratch-disk-001 --reviewer "Your Name" --knowledge demo-team.json --yes
circa investigate-log examples/demo_disk_full.log --knowledge demo-team.json

# 3–4. Unknown failure: one local LLM call; the diagnosis is saved as a pending_review draft.
circa investigate-log examples/demo_unknown_failure.log --knowledge demo-team.json --synthetic

# 5. Review: list, inspect, optionally edit, then approve (or reject).
circa knowledge list --pending --knowledge demo-team.json
circa knowledge show draft-06a3cf72d814 --knowledge demo-team.json
circa knowledge show draft-06a3cf72d814 --knowledge demo-team.json --json > draft.json
# ...edit draft.json, then:
circa knowledge update draft.json --knowledge demo-team.json --yes
circa knowledge verify draft-06a3cf72d814 --reviewer "Your Name" --knowledge demo-team.json --yes \
  --category infrastructure \
  --validation-method repeated_runs_passed \
  --validation-evidence "Synthetic example: 3 consecutive packaging runs passed after renewal"
# or: circa knowledge reject draft-06a3cf72d814 --reviewer "Your Name" --knowledge demo-team.json

# 6. The approved knowledge now answers the same failure, with no LLM call.
circa investigate-log examples/demo_unknown_failure.log --knowledge demo-team.json
```

The draft ID is derived from the failure's dedup fingerprint, so the same log
always gives the same ID. Drop `--synthetic` for real failures. `--json` prints
the whole investigation.

**Approving is not validating.** `circa knowledge verify` accepts the record as
reviewed knowledge. The fix becomes `validated` only when you supply
`--validation-method` and `--validation-evidence` describing validation you
actually did. A real (non-synthetic) incident cannot be approved without them,
so it stays `pending_review`. A synthetic draft approved without them keeps an
unvalidated fix: later investigations show it as an existing record but never
as a known verified solution. A historical fix was validated for the recorded
incident, not for your environment; confirm it applies.

**Category review.** `draft_origin.category_source` records where a draft's
category came from: `deterministic` (analyzer), `historical` (analyzer via
matched incidents), `llm` (the analyzer found none), or `reviewer`. A draft
whose category came from the LLM cannot be approved until the reviewer confirms
or corrects it with `--category`, which records `reviewer`. Changing the
category through `show --json` and `update` records `reviewer` too. CIRCA sets
this field itself and ignores edits to it. Records without `draft_origin` are
unaffected.

Draft evidence holds the exact log lines with a failure signal, plus a line
that directly continues one (the next line, when it does not start its own
timestamped, bracketed, or `LEVEL:` record), such as the second line of a
multi-line error.

### Piloting on real logs

Evaluate diagnosis quality on sanitized real logs with the existing commands
only, in an ignored `pilot/` directory so nothing is committed or written to
team knowledge:

```bash
mkdir -p pilot && cp team/incidents.json pilot/knowledge.json   # isolated copy; never the original
# Copy each sanitized log into pilot/ (no secrets, customer data, or private URLs).
circa investigate-log pilot/failure.log --knowledge pilot/knowledge.json --json > pilot/failure.json
circa knowledge list --pending --knowledge pilot/knowledge.json
circa knowledge show DRAFT-ID --knowledge pilot/knowledge.json
```

Record one row per log, keeping automated fields separate from human
judgment:

| Field                                                    | Source                                                                                                                                                                   |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| failure type                                             | network, unit-test assertion, disk space, certificate or signing, hardware or device initialization, build or release infrastructure                                     |
| confirmed root cause                                     | independently confirmed (postmortem, fix commit, or owner) with a reference; otherwise `inconclusive`. An `Unconfirmed hypothesis` in team knowledge is not ground truth |
| `analysis.category`, `outcome`, `llm_called`, `evidence` | `--json` output (automated)                                                                                                                                              |
| draft created, `category_source`, `notes`                | `--json` output (automated): ignored quotes and omitted suggestions                                                                                                      |
| hypothesis, category, and fix correct?                   | reviewer judgment, only for cases with a confirmed root cause                                                                                                            |

Do not run `knowledge verify` in a pilot unless a reviewer actually validated
the fix, and never run a suggested fix from the output. To check reuse,
approve in the pilot copy with real validation evidence, then rerun
`investigate-log` on a later log of the same failure. Do not report accuracy
from a handful of logs.

## Team incident knowledge

The packaged incidents and the retrieval evaluation dataset are synthetic and
read-only. A team records real, human-reviewed incidents in its own JSON file,
for example kept in a repository so every change is reviewed in a pull request,
and passes it explicitly:

```bash
circa analyze examples/hardware_failure.json --knowledge team/incidents.json
circa analyze-log examples/demo_disk_full.log --knowledge team/incidents.json
```

Without `--knowledge`, analysis and retrieval are unchanged. With it, only
`verified` incidents from the file are matched alongside the packaged ones, and
a matched verified incident's validated `root_cause` is reported. `analyze` and
`analyze-log` never write the file; `investigate-log` only adds
`pending_review` drafts to it; `circa benchmark` never reads it.

### Lifecycle

```text
pending_review ──(circa knowledge verify)──► verified ──(circa knowledge supersede)──► superseded
       └──────(circa knowledge reject)──► rejected
```

```bash
circa knowledge add examples/incident_draft.json --knowledge team/incidents.json
circa knowledge update revised_draft.json --knowledge team/incidents.json
circa knowledge verify INCIDENT-ID --reviewer "Reviewer Name" --knowledge team/incidents.json
circa knowledge supersede OLD-ID --by NEW-ID --knowledge team/incidents.json
circa knowledge reject INCIDENT-ID --reviewer "Reviewer Name" --knowledge team/incidents.json
circa knowledge show INCIDENT-ID --knowledge team/incidents.json [--json]
circa knowledge list --knowledge team/incidents.json [--pending]
circa knowledge validate team/incidents.json
```

- A draft is a human-written incident object; see the synthetic
  `examples/incident_draft.json`. `add` always stores it as `pending_review`
  and computes its deduplication fingerprint. Drafts may not set `status`,
  `fingerprints`, `reviewed_by`, `superseded_by`, `created`, or `updated`.
- Only `pending_review` incidents can be updated, approved, or rejected. A
  verified incident is replaced by adding a new incident and superseding the
  old one. Rejected drafts are kept for the record but never matched.
- `show --json` prints an editable draft (managed fields removed) for `update`.
- Every write shows the record and asks for confirmation (or takes `--yes`),
  then writes atomically. It refuses if the file changed since it was read.
- Commands exit with status 1 on any validation error or declined write.

### Schema rules

- `synthetic` records provenance; `status` records the lifecycle. Team records
  must state `status` explicitly.
- A real incident can be `verified` only with a `root_cause`, a `reviewed_by`
  reviewer, and a `fix` whose `validation_status` is `validated` with a
  `validation_method` and `validation_evidence`. Allowed methods are
  `reproduced_then_fixed`, `regression_test_added`, `repeated_runs_passed`
  (the evidence must state at least 3 runs), and `manual_verification`. A
  single successful retry is not a validation method.
- LLM output never validates a fix. `investigate-log` drafts store the LLM's
  hypothesis as `root_cause` and an `unvalidated` fix, marked by
  `draft_origin.ai_generated`. Only a human reviewer's `--validation-*` options
  or an edited draft can mark a fix `validated`. `--llm` results are never stored.
- `evidence` holds at most 10 single-line excerpts of at most 300 characters,
  never raw logs. Text matching obvious credential patterns (private-key
  headers, AWS, GitHub, and Slack tokens, `password=`-style assignments) is
  rejected. This is a safety net, not a substitute for review and Gitleaks.
- `superseded_by` must be set exactly when `status` is `superseded` and must
  name an incident in the same file. IDs may not repeat or collide with
  packaged incident IDs.

### Duplicate detection

- **Exact duplicates** share a deduplication fingerprint and are rejected. The
  fingerprint is a SHA-256 of the incident's `evidence` lines after lowercasing,
  collapsing whitespace, sorting, and removing duplicates. It also replaces
  timestamps and clock times, UUIDs, hex addresses and hashes of 7+
  characters, temporary paths, durations, separator-delimited numeric suffixes
  (`ci-linux-17`), and build, run, job, PID, or `#` numbers. Leading log-level
  labels are dropped. Exit codes, HTTP status codes, ports, versions, and
  module names are kept, so `exit code 1` and `exit code 137` stay distinct.
- **Near duplicates** are found with the existing keyword scoring against every
  incident in the team file, including pending and superseded ones. A score of
  4 or more is reported, and the write requires `--allow-similar`.
- The primary `fingerprint` in analysis output is unchanged.

Limitations: the dedup fingerprint only matches evidence that normalizes to the
same lines, so reworded evidence relies on the near-duplicate check. Near
duplicates are not checked against packaged synthetic incidents. Files written
by CIRCA are stable, sorted JSON but are not Prettier-formatted; run Prettier on
the file, or exclude it, if the repository that stores it checks formatting.
Lifecycle dates are recorded in UTC.

## Retrieval benchmark

The version-controlled packaged `circa/data/retrieval_evaluation.json` dataset contains only
synthetic cases. Labels identify historical incident IDs only when the local
incident dataset supports that relationship; ambiguous cases have no relevance
label and are excluded from Top-1, Recall@K, and MRR denominators.

```bash
circa benchmark
circa benchmark --top-k 5
```

The command always benchmarks keyword retrieval. Semantic and combined results
use the optional local Ollama embedding backend (`nomic-embed-text` by default)
when available. If it is unavailable, semantic metrics are marked unavailable
and combined results explicitly report keyword fallback; no cloud services are
used. Top-1, Recall@K, and MRR use only cases with known relevant incidents;
no-result rate uses evaluated cases and coverage uses all dataset cases.

The incident and evaluation datasets ship with the CIRCA wheel, so `circa
benchmark` works after a normal installation as well as an editable install.
