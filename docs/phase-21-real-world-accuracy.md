# Phase 21 — Real-World Accuracy Evaluation

Evaluated at commit `b086262` (v0.2.2), 2026-11-01. Deterministic analysis only;
no LLM was called. No analyzer code, knowledge record, or benchmark expectation
was changed.

**Bottom line:** the real-world sample is far too small to estimate accuracy:
6 real failures with an independently established root cause, from 3 distinct
incidents, only 2 of them unseen while the rules were written. It is still
large enough to show one serious defect. When a failure's own incident is
held out, every historical match CIRCA makes is false, and in each one CIRCA
reports the other incident's root cause as this failure's root cause.

## Methodology

- **Real and synthetic results are reported separately and never combined.**
  The existing `circa evaluate` total, "All confirmed labels", mixes 4 real
  logs into synthetic cases; it is not used here.
- **Ground truth:**
  - A real case is "confirmed" only if a verified team incident describes it,
    or its fix was validated by a passing rerun.
  - `pending_review` incidents and labels are reported separately and are not
    ground truth.
- **Seen versus unseen:** a log counts as "seen in development" if it was
  available when the current analyzer rules were written. The rules were
  written in Phases 14–16B, and the logs in `evaluation/logs/` were added in
  Phase 15. Seen logs are not a test of generalization.
- **Holdout (leave-one-out),** `evaluation/holdout.py`:
  - for each real failure, its own team incident is removed from an in-memory
    copy of `team/incidents.json`;
  - `investigate_log` runs against the remaining verified team incidents plus
    the packaged synthetic incidents;
  - the script asserts that the held-out incident can never be matched,
    returned, or quoted, and that the team file's bytes are unchanged
    (`tests/test_holdout_evaluation.py`).
- **Synthetic:**
  - the existing `circa evaluate` (classification) and `circa benchmark`
    (retrieval) commands, unchanged;
  - these use only the packaged synthetic incidents, never team knowledge.

## Dataset composition

| Set                                             | Cases  | Independent incidents | Ground truth                                                            | Seen in development |
| ----------------------------------------------- | ------ | --------------------- | ----------------------------------------------------------------------- | ------------------- |
| Real, confirmed                                 | 6 logs | 3                     | 5 logs from 2 verified team incidents; 1 fix-validated (no team record) | 4 seen, 2 unseen    |
| Real, pending review                            | 3 logs | 3                     | Log evidence only                                                       | 3 seen              |
| Synthetic log cases (`evaluation/cases.json`)   | 21     | 21                    | By construction                                                         | n/a                 |
| Synthetic quality-gate cases                    | 10     | 10                    | By construction                                                         | n/a                 |
| Legacy synthetic retrieval/classification cases | 35     | 35                    | By construction                                                         | n/a                 |

**Real, confirmed.** These are all 5 failed runs in `scmbuildrelease/CIRCA`
up to 2026-11-01. Run 39988949831 contributes 4 job logs of one incident:
Prettier, Ruff format, Bandit and mypy.

The 2 unseen confirmed failures are:

- run 41331905922, Prettier on committed release-notes scratch files (verified
  incident `real-circa-ci-prettier-scratch-files-001`);
- run 41345998382, zizmor flagging a vulnerable pinned action, fixed by
  `b086262` (rerun 41346114303 passed).

**Pending review.** Ruff import sorting, a pytest assertion, and the tt-metal
protected-tag failure. The tt-metal incident itself is still
`pending_review`.

All real logs come from 2 repositories, both on GitHub Actions, and 8 of the 9
come from CIRCA's own CI. Nothing here represents other CI systems or other
kinds of projects.

## Real-world results (holdout)

Each rate is numerator/denominator, with a 95% Wilson interval where it means
anything.

| Metric (definition)                                       | Confirmed (n=6)     | Unseen only (n=2) | Pending (n=3) |
| --------------------------------------------------------- | ------------------- | ----------------- | ------------- |
| Category accuracy (predicted = expected)                  | 5/6 (0.44–0.97)     | 1/2               | 2/3           |
| Unknown rate (predicted `unknown`)                        | 1/6                 | 1/2               | 0/3           |
| False-match rate (any historical match ÷ cases; see note) | **3/6** (0.19–0.81) | 1/2               | 2/3           |
| Another incident's root cause reported as this failure's  | **3/6**             | 1/2               | 2/3           |
| No-match rate                                             | 3/6                 | 1/2               | 1/3           |
| Wrong known solution returned (outcome `known_solution`)  | 0/6                 | 0/2               | 0/3           |

**Note on false matches.** After holdout, no remaining incident shares any of
these failures' root causes, so every match counts as false.

**Not calculable on real data:**

- Top-1 retrieval accuracy, Recall@3 and MRR. Under holdout no failure has a
  relevant incident left in the knowledge base (every incident has a distinct
  cause), so the denominator is 0. Measuring retrieval needs at least two
  independent failures of the same incident.
- Owner accuracy. Owner is a fixed mapping from the category
  (`_OWNERS` in `src/circa/analyzer.py`), and there are no independent owner
  labels, so it would only repeat category accuracy.

**Without team knowledge** (`circa evaluate`, packaged incidents only), the
same real logs score as follows:

- Category: confirmed 4/4, pending 2/3.
- Root cause containing the labeled terms: confirmed 3/4. The miss is a
  labeling artifact: the expected term `urlopen` versus the log's wording
  `url open` in Bandit B310.

## Synthetic results (existing commands, packaged incidents)

| Set                | Category accuracy | Unknown | False positives (expected unknown) | First match relevant | Root-cause terms |
| ------------------ | ----------------- | ------- | ---------------------------------- | -------------------- | ---------------- |
| Synthetic logs     | 17/21 (0.60–0.92) | 8/21    | 0/4                                | 7/10                 | 12/15            |
| Quality-gate cases | — (see note)      | —       | —                                  | —                    | 6/6              |
| Legacy             | 27/35 (0.61–0.88) | 14/35   | 0/7                                | 20/24                | n/a              |

Note: the quality-gate run's "All confirmed labels" row combines its 10 cases
with the 35 legacy cases (36/45 category accuracy). The 10 cases alone are not
broken out by the existing command.

Retrieval (`circa benchmark`, keyword, 35 cases, 24 with a labeled relevant
incident):

- Top-1 is 22/24 = 0.917 (0.74–0.98), Recall@3 is 23/24 = 0.958, and MRR is
  0.938.
- The no-result rate is 4/35.

Each synthetic query was written alongside the incidents it is meant to match,
so these figures show that the scoring works as designed. They don't show how
well it generalizes.

## Failure analysis

| Failure                                     | Expected                                        | CIRCA under holdout                                                                                      | Cause                                                                                                                               | Type                                                 |
| ------------------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 39988949831 Prettier, Ruff format           | Code not yet formatted when gates were added    | `software`; matched the scratch-files incident; root cause "Commit fc57e37 included notes.md…"           | The overlap is the tool's own output words (`prettier`, `code`, `style`, `format`, `found`), which describe the tool, not the cause | **Analyzer defect** (matching and root-cause reuse)  |
| 41331905922 Prettier scratch files (unseen) | Scratch files committed by mistake              | `software`; matched the baseline incident; root cause "Commit 43c1910 added Prettier, Ruff…"             | Same mechanism, in the other direction                                                                                              | **Analyzer defect**                                  |
| tt-metal protected tag (pending)            | Infrastructure: a ruleset rejected a tag update | **`software`**; matched both CIRCA incidents; root cause about CIRCA's notes.md                          | Overlap `found`, `release`, `repository`: 3 incidental words. Without team knowledge the result is `unknown`                        | **Analyzer defect**, which made the category wrong   |
| 39859422492 Ruff import sort (pending)      | Unsorted imports                                | `software`; matched the baseline incident; root cause about Prettier, Ruff format, mypy and Bandit gates | Overlap `code`, `found`, `quality`                                                                                                  | **Analyzer defect**                                  |
| 41345998382 zizmor advisory (unseen)        | Pinned action with a known vulnerability        | `unknown`                                                                                                | No rule covers workflow-audit findings, and no incident exists. `unknown` is the safe answer                                        | Missing knowledge or rule coverage; one example only |
| 39988949831 Bandit (labels)                 | B310                                            | Correct, but the root-cause term check fails                                                             | Term `urlopen` versus log `url open`                                                                                                | Evaluation-dataset limitation                        |

**Mechanism.** `analyze_failure` (`src/circa/analyzer.py`) works as follows:

1. It takes the top keyword matches.
2. It accepts a category when the strongest match shares at least 2 tokens
   outside a 12-word generic list.
3. If that match's root cause passes `_root_cause_applies`, which only checks
   the dependency-installation stage, it then **replaces** CIRCA's own
   diagnosis with that incident's `root_cause` and `recommendation`.

Real team incidents carry about 30–38 distinctive tokens each, against a median
of 10.5 for the packaged ones, so incidental two-word overlaps are common. For
failures whose explicit error is already recognized, such as Prettier, the
replacement swaps a correct, specific diagnosis ("A formatting check
failed…") for a wrong incident-specific one. This gets worse as the team
knowledge base grows.

**Why the existing evaluation missed it:** `circa evaluate` never loads team
knowledge. The `known_solution` path is safe because it requires the
incident's recorded evidence lines in the log (0 wrong known solutions); the
analyzer's ordinary matching path has no such check.

The root-cause term check also misses it: under holdout the wrong root cause
for the 39988949831 Prettier log still contains `format`, so it would be
scored correct.

## Known limitations

- **Sample size.** n=6 confirmed real cases (2 unseen) from 3 incidents in 2
  repositories. The intervals above span roughly half the scale. No rate here
  describes CIRCA's accuracy in general.
- **Correlated cases.** The 4 logs from run 39988949831 are one incident, so
  they are not independent observations.
- **Development leakage.** 7 of the 9 real logs were available while the rules
  were written.
- **Single labeler.** Real labels were checked by one maintainer.
- **No retrieval measurement.** Retrieval metrics on real data are undefined
  until the knowledge base holds repeated incidents.

## Prioritized recommendations

1. **Use a team incident's root cause and recommendation only when its
   evidence is observed.** Otherwise list it only as a similar incident.
   - Reuse the existing `known_solution` evidence check: the incident's
     recorded evidence lines must appear in the log, after normalization.
   - When the explicit-error rule fired, keep its specific root cause.
   - Assessment: this removes all 6 wrong root causes observed. Complexity is
     low, since it reuses existing code. The false-positive risk goes down.
     Maintenance is low.
2. **Apply the same condition before a team incident decides the category.**
   This fixes the tt-metal miscategorization while keeping `unknown` as the
   safe default. Complexity is low; the risk is that a few true matches become
   `unknown`, which the holdout measures.
3. **Make holdout part of routine evaluation.** Run `evaluation/holdout.py` in
   CI, and report its numbers alongside `circa evaluate`. Complexity is low.
   This keeps the defect class from returning unseen.
4. **Grow the real-world set before tuning anything else.**
   - Review the tt-metal draft.
   - Record the zizmor failure as an incident.
   - Collect failures from other repositories, aiming for at least 20
     independent incidents, including repeats of the same incident.
   - Retrieval metrics and rule changes (zizmor or workflow-audit coverage,
     GitHub ruleset rejections) should wait for that data. One example each
     is not evidence.
5. **Fix the root-cause term label for Bandit B310** (`urlopen` versus
   `url open`). This is a label change only, done in a reviewed change, not
   to improve a score.

## Proposed acceptance criteria for the next phase

- **Holdout:** another incident's root cause reported, 0/6 confirmed and 0/3
  pending (currently 3/6 and 2/3).
- **Holdout:** a team incident matched without its evidence observed, 0
  (currently 6/9).
- **Holdout:** tt-metal not classified `software`; `unknown` or
  `infrastructure` is acceptable.
- **Synthetic, unchanged:**
  - `circa evaluate`: 17/21 synthetic logs, 27/35 legacy, 0 false positives;
  - `circa benchmark`: Top-1 22/24, Recall@3 23/24, MRR 0.938;
  - real logs without team knowledge, confirmed 4/4.
- **Wrong known solutions:** stay at 0.
- **No changes to** verified records, labels or synthetic expectations to meet
  these criteria.

## Reproducing

```bash
# Synthetic and packaged-only results
circa evaluate
circa evaluate --dataset evaluation/quality_gate_cases.json
circa benchmark

# Real-world holdout. Raw CI logs are not stored in the repository; fetch the
# unseen runs into a temporary directory first.
LOGS=$(mktemp -d)
for run in 41331905922 41345998382; do
  gh run view "$run" -R scmbuildrelease/CIRCA --log-failed > "$LOGS/circa-$run.log"
done
python evaluation/holdout.py --log-dir "$LOGS"          # add --json for per-case output
python evaluation/holdout.py                            # repository logs only; unseen runs are listed as skipped
```
