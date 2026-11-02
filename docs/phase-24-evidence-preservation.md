# Phase 24 — Real CI Evidence Preservation

**Bottom line:** every real CI log used by the holdout evaluation is now stored
in the repository as a sanitized copy, with its source run recorded in
`evaluation/logs/manifest.json`. `python evaluation/holdout.py` no longer
contacts GitHub. Its results are identical to Phase 23's.

**What did not change:**

- analyzer code;
- incident records, including verified ones;
- the 7 log files already stored, which are byte-identical;
- `circa evaluate`, `circa benchmark` and their fingerprints;
- dependencies.

## Current retention limitations (before this phase)

- **Logs expire.** By default GitHub deletes Actions logs after 90 days. Two
  holdout cases (runs 41331905922 and 41345998382, one of them a verified
  incident) existed only on GitHub and were fetched with `--log-dir` on each
  run.
- **Knowledge records keep only part of the source.**
  - A record keeps up to 5 evidence lines and the run in `environment`.
  - `draft_origin.source_log` names the log but does not store it.
  - That is enough for matching, but not for re-running an evaluation.
- **No provenance for the stored logs.** The 7 logs from Phase 15 in
  `evaluation/logs/` had no record of their run, job, capture time or incident.
  Each case's run was typed by hand in `evaluation/holdout.py`.
- **No integrity check:** an edited log would silently change the results.

## Local storage format

The existing `evaluation/logs/` directory is the store. There's no database,
no external service and no new dependency.

- **One sanitized file per job and run:** `<repo>-<run>-<slug>.log` for new
  copies. Existing names are kept, so `evaluation/cases.json` is unchanged.
- **`manifest.json`:** one entry per file:

```json
{
  "file": "circa-41331905922-prettier-scratch-files.log",
  "repository": "scmbuildrelease/CIRCA",
  "run": "41331905922",
  "job": "Format (Prettier)",
  "captured_at": "2026-11-02T00:21:50+00:00",
  "incident": "real-circa-ci-prettier-scratch-files-001",
  "evidence": ["… [warn] notes.md", "…"],
  "sha256": "256f86cc…"
}
```

- **`evidence`** holds the incident's evidence lines that occur in this file.
  For example, each of the 4 job logs of run 39988949831 holds 1 of its
  incident's 4 lines.
- **`incident`** may be a verified record, a pending record or a draft in
  `team/drafts/`. Ground truth is still decided only by verified records.
- **The 7 earlier logs:**
  - their `captured_at` is the time of commit `b0e5d2d`, which added them; the
    actual capture time was not recorded;
  - the tt-metal file is a reviewer-supplied excerpt, not a `gh` export, and
    has a `note` saying so.

## Implementation changes

- **`evaluation/preserve.py` (new):**
  - **Input:** `python evaluation/preserve.py RUN_URL --incident ID --name SLUG [--job NAME] [--from-file LOG]`.
  - **Fetching:** it uses CIRCA's own `fetch_failed_log`, or a file already
    fetched with `gh`.
  - **Sanitizing:** it removes colour codes, keeps the lines of one job (and
    refuses to guess if there are several), and redacts with `redact_secrets`.
    Redaction fails closed; it never stores the original values.
  - **Before writing:** it checks that every evidence line of the incident is
    in the sanitized log, and it refuses to overwrite.
  - **Output:** it records the manifest entry with a SHA-256 checksum.
- **`evaluation/holdout.py`:**
  - each case names a preserved file, and the case's run comes from the
    manifest, not from hand-typed source strings;
  - each log's checksum is verified before use;
  - the exclusion and ground-truth rules from Phase 23 are unchanged;
  - `--log-dir` and the `run:` cases were removed;
  - the Phase 21 and 23 instructions now point here.
- **New preserved logs:** runs 41331905922 and 41345998382. Each is
  byte-identical to the Phase 23 fetch with colours removed. I read both: the
  only token in them is `GH_TOKEN: ***`, already masked by GitHub.

## Security and redaction tests

`tests/test_evidence_preservation.py` has 6 tests:

- **Every preserved log** is listed in the manifest with a matching checksum.
  It contains no recognized credential and no colour codes, holds only its
  recorded job, and contains its recorded evidence.
- **Run association:** each entry's incident (team record or draft) was
  recorded from that same run (`environment.repository` and `environment.run`,
  or a fix reference to the run). Its evidence is a subset of the incident's.
- **Sanitizing:**
  - a synthetic GitHub token and a URL password containing `@` become
    `[REDACTED]`;
  - colour codes and other jobs' lines are removed;
  - the manifest entry is exact;
  - the holdout reads the file back locally, with its run.
- **Integrity:** an edited log (checksum mismatch) and an unlisted file are
  refused.
- **Refusals:**
  - a run with several jobs and no `--job`;
  - a job whose lines lack the evidence, or an unknown job;
  - overwriting an existing copy;
  - nothing is written when a refusal happens.
- **Holdout coverage:** every holdout case reads a preserved log.

`tests/test_holdout_evaluation.py` now runs without network access. It checks
that exclusion still removes each run's own records, that one run counts as one
incident, and that a draft verified later is excluded from its own evaluation.

## Holdout reproducibility

```bash
python evaluation/holdout.py           # no GitHub access; --json for per-case output
```

| Group                        | Logs | Incidents | Category correct | False team matches | Adopted root causes | Wrong known solutions |
| ---------------------------- | ---- | --------- | ---------------- | ------------------ | ------------------- | --------------------- |
| Verified ground truth        | 5    | 2         | 2/2              | 0                  | 0                   | 0                     |
| Not ground truth (no record) | 4    | 4         | 2/4              | 0                  | 0                   | 0                     |

These are the same per-case results as Phase 23 with `--log-dir`. The
difference is that the two GitHub-only logs no longer need fetching and can't
expire. `circa evaluate` (both datasets) and `circa benchmark` produce
byte-identical output.

## Remaining limitations

- **Redaction is pattern-based** (see `SECURITY.md`). Review a preserved log
  before committing it, as the script reminds you. It may also contain private
  paths or internal hostnames, which redaction doesn't cover.
- **Only `--log-failed` output is preserved,** not the full run log. That is
  enough for every current incident.
- **Preservation is a manual step,** done when a draft is created or verified.
  `circa knowledge verify` doesn't capture the log itself; that would need a
  CLI change and wasn't requested.
