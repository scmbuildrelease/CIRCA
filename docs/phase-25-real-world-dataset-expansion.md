# Phase 25 — Real-World Incident Dataset Expansion

**Bottom line:** CIRCA now has **6 independent, verified real incidents**, up
from 2. They cover 9 logs from 2 repositories. The milestone of 8 was not
reached, and every verified incident is still `software`. With the new
incidents in the holdout, CIRCA still makes no false team match, borrows no
root cause and returns no wrong known solution.

No analyzer, retrieval, synthetic or existing evidence file was changed.

## What was inspected

- **Docs:** Phase 21, 23 and 24 reports. The Phase 23 file is
  `docs/phase-23-real-world-dataset.md`; there is no
  `phase-23-real-world-dataset-expansion.md`.
- **Data:**
  - `evaluation/cases.json`;
  - `evaluation/logs/manifest.json`, with 9 preserved logs;
  - `team/incidents.json`: 2 verified, 1 pending;
  - `team/drafts/`: 4 drafts.
- **CI:** `scmbuildrelease/CIRCA` has had no new failed runs since Phase 23, so
  no new CIRCA cases exist.

## Newly verified incidents

Iris Wang reviewed and verified these with `circa knowledge add` and
`circa knowledge verify`. Each one has a preserved failing log, a root cause
confirmed by the fix, and the same job passing in the next run that includes
the fix.

| Incident                                        | Failing run                        | Fix                     | Same job passed                              | How the cause was confirmed                                                                                                                               | Method                |
| ----------------------------------------------- | ---------------------------------- | ----------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `real-circa-ci-ruff-import-sort-001`            | CIRCA 39859422492 (`351979c`)      | `bee8ff2`               | 39980112618                                  | `ruff check --select I` gives I001 on the file at `351979c` and passes at `bee8ff2`                                                                       | reproduced_then_fixed |
| `real-circa-ci-pytest-rich-usage-assertion-001` | CIRCA 41312342881 (`c4b5de5`)      | `60731a9`               | 41312876040                                  | The test fails at `c4b5de5` only with `GITHUB_ACTIONS=true`, and passes at `60731a9` either way                                                           | reproduced_then_fixed |
| `real-circa-ci-zizmor-vulnerable-action-001`    | CIRCA 41345998382 (`e651c26`)      | `b086262`               | 41346114303                                  | The fix changes only the vulnerable pin (v1.14.2 → v1.14.3) named by the advisory                                                                         | manual_verification   |
| `real-tt-metal-blaze-prefill-import-error-001`  | tt-metal 40452853696 (`c7b41d5c7`) | `c30746f89` (PR #62673) | 40635507280 (first nightly run with the fix) | The PR cites the run. At `c7b41d5c7`, `tt_ccl.py` defines only `_SPARSE_MLA_OVERLAP_GATHER_ROWS`, while the test imports `sparse_mla_overlap_gather_rows` | manual_verification   |

**Notes on these incidents:**

- **tt-metal job choice:** run 40452853696 failed 18 jobs for several reasons.
  Only the GLM chunked-accuracy job is used as evidence: its log contains the
  `ImportError`, and that job passed in 40635507280. The same job's later
  failure, in run 40820921702, is a different error.
- **Near-duplicate warnings:** `circa knowledge add` flagged keyword
  similarity with the existing records. Each case has a different cause, so
  `--allow-similar` was used after checking.
- **Existing records:** the records that were already in the file are
  byte-identical.

## Independent count and coverage

| Repository            | Verified incidents | Logs | Categories |
| --------------------- | ------------------ | ---- | ---------- |
| scmbuildrelease/CIRCA | 5                  | 8    | software   |
| tenstorrent/tt-metal  | 1                  | 1    | software   |

- **Totals:** 6 independent incidents from 9 logs. The 4 job logs of CIRCA run
  39988949831 count as one incident.
- **Still missing:** hardware, network and infrastructure ground truth; any
  non-GitHub CI; and any repeated incident, so real retrieval recall is still
  undefined.

## Pending: `real-tt-metal-protected-tag-update-001`

This record stays `pending_review`. The full job log is still available, and
it confirms how the failure happened:

- the workflow found tag `v0.81.1` already at `07d1af7`;
- it then tried to move the tag to `d80d948`;
- GitHub's protected-ref rule rejected the update.

There is no fix and no validated resolution. The workflow file hasn't changed
the tag logic since, and later runs of the release workflow still fail.

**A defect in the earlier evidence, recorded rather than changed:**

- the record's `environment.release_ref` and the Phase 15 excerpt
  (`tt-metal-protected-tag.log`, line `Release tag: v0.81.1-rc2`) point at
  `v0.81.1-rc2`;
- that is the latest RC tag, not the tag being moved;
- neither was edited.

A new excerpt, `tt-metal-40072645356-protected-tag-error-step.log`, preserves
the actual lines. A reviewer should correct the record with
`circa knowledge update` and leave it pending until a fix is known.

## Evidence preservation

- **New logs**, both recorded by `evaluation/preserve.py` with an explanatory
  `note`:
  - `tt-metal-40452853696-blaze-prefill-import-error.log`: lines 1466–1502 of
    job 122181874118.
  - `tt-metal-40072645356-protected-tag-error-step.log`: lines 12859–12871 of
    the failed job.
- **Why excerpts:** the full Blaze job log names self-hosted runner hosts, an
  internal IP address, Helm release names and model-storage mounts, so it is
  not preserved.
- **Review:** both excerpts were checked by hand and by pattern for IP
  addresses, emails, internal hosts, mounts and credentials; none were found.
  The only paths are standard `/home/runner` and `/opt/venv` paths.
- **Capture times** are exact. The 7 Phase 15 logs keep their approximate
  commit time, as documented in Phase 24.
- **Tooling:** `preserve.py` gained a `--note` option, with a test. The
  existing preserved logs and manifest entries are unchanged; 2 entries were
  added.

## Holdout results

`python evaluation/holdout.py` runs offline. All logs from one run are excluded
together, and only verified records count as ground truth.

|                                      | Phase 24 | Phase 25                                                                                             |
| ------------------------------------ | -------- | ---------------------------------------------------------------------------------------------------- |
| Ground truth: logs / incidents       | 5 / 2    | 9 / 6                                                                                                |
| Category correct (incidents)         | 2/2      | 5/6                                                                                                  |
| Unknown category (incidents)         | 0/2      | 1/6 (zizmor)                                                                                         |
| False team matches                   | 0        | 0                                                                                                    |
| Borrowed root causes                 | 0        | 0                                                                                                    |
| Wrong known solutions                | 0        | 0                                                                                                    |
| Packaged synthetic match (incidents) | 0/2      | 2/6 (the pytest and Blaze `ImportError` cases matched packaged synthetic incidents of the same kind) |
| Not ground truth: logs / incidents   | 4 / 4    | 2 / 1 (tt-metal tag)                                                                                 |

- **Why the numbers moved:** CIRCA's prediction for every log that was already
  in the holdout is unchanged. Only the grouping changed, because 3 runs gained
  verified records, and 2 tt-metal logs were added.
- **Self-match sanity check:** when a log's own record is not excluded, 5 of
  the 6 incidents return themselves as a known solution. The quality-gate
  baseline can't, because its evidence is only tool summaries (see Phase 22).
  This is a sanity check, not a recall measurement.
- **Synthetic results:** `circa evaluate` (both datasets) and
  `circa benchmark` are byte-identical to before.

## Limitations and gaps

- **Count:** 6 of 8 for the milestone, and 6 of 20 long-term.
- **Coverage:** 5 of the 6 come from CIRCA's own CI, and all are
  formatting, lint, test or dependency failures.
- **Category and owner:** both are still `software` for every verified case.
  The only infrastructure case is unresolved.
- **Sample size:** n=6 can't support accuracy claims; one wrong case moves
  category accuracy by 17 points.
- **Sources:** tt-metal has many failures, but few have a cited fix and a clean
  passing rerun of the same job. A hardware-runner failure without a fix
  (device hangs and teardown timeouts are common) is not verified.

## Next steps

1. Correct and keep pending the tt-metal tag record (above).
2. Look for tt-metal infrastructure or hardware failures whose fix or
   maintainer explanation is recorded, such as a PR that cites a run. Search
   `[Bug fix]` and `[BugFix]` PRs with a "Problem" section that links a run.
3. Add a second occurrence of an existing incident so retrieval can be
   measured.

## Reproduce

```bash
circa knowledge validate team/incidents.json
python evaluation/holdout.py            # offline; --json for per-case output
circa evaluate && circa evaluate --dataset evaluation/quality_gate_cases.json
circa benchmark
```
