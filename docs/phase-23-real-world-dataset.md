# Phase 23 — Real-World Incident Dataset

Inventory at commit `607cfc7`, 2026-11-01. No analyzer code or existing
incident record was changed.

**Bottom line:** CIRCA has **2 independent, verified real incidents**. The
target is 20. Three more are prepared as reviewable drafts with real evidence
and fix commits; verifying them would bring the count to 5. Every real case is
`software` and comes from GitHub Actions, and 8 of the 9 real logs come from
CIRCA's own repository.

## Current inventory

| Run (repository)                  | Logs                             | Team record                                                      | Status                                  | Ground truth? |
| --------------------------------- | -------------------------------- | ---------------------------------------------------------------- | --------------------------------------- | ------------- |
| scmbuildrelease/CIRCA 39988949831 | 4 job logs in `evaluation/logs/` | `real-circa-ci-quality-gate-baseline-001`                        | verified                                | **Yes**       |
| scmbuildrelease/CIRCA 41331905922 | fetched on demand                | `real-circa-ci-prettier-scratch-files-001`                       | verified                                | **Yes**       |
| scmbuildrelease/CIRCA 39859422492 | `evaluation/logs/`               | draft `team/drafts/circa-ruff-import-sort.json` (new)            | not recorded                            | No            |
| scmbuildrelease/CIRCA 41312342881 | `evaluation/logs/`               | draft `team/drafts/circa-pytest-rich-usage-assertion.json` (new) | not recorded                            | No            |
| scmbuildrelease/CIRCA 41345998382 | fetched on demand                | draft `team/drafts/circa-zizmor-vulnerable-action.json` (new)    | not recorded                            | No            |
| tenstorrent/tt-metal 40072645356  | `evaluation/logs/`               | `real-tt-metal-protected-tag-update-001`                         | pending_review (root cause unconfirmed) | No            |

These are all 5 failed runs in `scmbuildrelease/CIRCA` to date, plus the one
tt-metal case. **Independent verified incidents: 2.** The 4 logs of run
39988949831 are one incident.

The three new drafts were written from the logs and git history only:

- every evidence line is copied verbatim from its log;
- each root cause is the change made by the fix commit (`bee8ff2`, `60731a9`
  and `b086262` respectively);
- each fix is followed by a passing CI run;
- each draft has at least one distinctive evidence line, in the Phase 22
  sense, and contains no recognized credential;
- the validation status is left for a reviewer to set.

They are proposals, not ground truth.

## Missing evidence and coverage gaps

- **Count:** 2 of 20 verified, or 5 of 20 if the drafts are verified.
- **Categories:** all verified cases are `software`; there is no hardware,
  network or infrastructure ground truth. The only infrastructure case
  (tt-metal) is unconfirmed.
- **Sources:**
  - repositories: 1 for the verified cases, 2 overall;
  - CI systems: GitHub Actions only;
  - generalization: 1 of the 2 verified incidents was unseen while the rules
    were written.
- **Distinctive evidence:** `real-circa-ci-quality-gate-baseline-001` records
  only tool summaries, so since Phase 22 it can't be matched. Fixing it would
  mean adding a corrected incident with file-specific lines (for example
  `src/circa/resources.py:13: error: …`), verifying it, and superseding the old
  one. That needs a reviewer; the record was not changed.
- **Log retention:** the two "fetched on demand" logs exist only on GitHub,
  which by default keeps Actions logs for 90 days. Sanitized copies should be
  added to `evaluation/logs/` (as in Phase 15) before then.
- **Owner:** records have no owner field. Owner is derived from the category
  (`_OWNERS` in `src/circa/analyzer.py`), so it can't be validated
  independently.
- **Retrieval metrics:** no incident has recurred, so Top-1, Recall@3 and MRR
  on real data remain undefined.

## Collection workflow (existing commands; nothing new needed)

```bash
# 1. Find and investigate a failed run (reads the log through `gh`; strips colors).
circa runs OWNER/REPO
circa investigate-log https://github.com/OWNER/REPO/actions/runs/RUN_ID --knowledge team/incidents.json
#    With a local Ollama model, a useful diagnosis is saved as a pending_review draft.
#    Without one, copy team/drafts/*.json or examples/incident_draft.json and fill it in by hand.

# 2. Review: confirm the root cause from the fix commit and a passing rerun, not from the LLM.
#    Evidence: copy 2–5 lines verbatim from the log, at least one naming a file, rule, module,
#    or version. Tool summaries alone ("Found 9 errors in 6 files") are not distinctive.
#    Set environment.repository and environment.run so evaluation can exclude the record.

# 3. Save as pending_review (it is never matched until verified).
circa knowledge add DRAFT.json --knowledge team/incidents.json     # add --allow-similar only after checking the reported similar incident

# 4. Verify with your own evidence (never an LLM output or a single retry).
circa knowledge verify INCIDENT_ID --knowledge team/incidents.json --reviewer "NAME" \
  --validation-method manual_verification \
  --validation-evidence "Run RUN_ID failed; fix COMMIT; run NEXT_RUN_ID passed."
circa knowledge validate team/incidents.json
```

To add the three prepared drafts, review each file in `team/drafts/` and run
steps 3 and 4.

## Evaluation integrity (`evaluation/holdout.py`)

- **Exclusion:** for each failure, every team incident recorded from the same
  run is removed. A record counts as from the same run if its
  `environment.repository` and `environment.run`, or a fix reference to the run
  URL, match. Exclusion is automatic, so a draft verified later can never match
  its own failure; a test simulates exactly that.
- **Ground truth:**
  - only a `verified` record for the run counts, and its category is the
    expected label;
  - pending records, drafts and cases without a record are reported separately
    as "not ground truth";
  - as a result, the zizmor case moved out of ground truth (fix validated, no
    verified record).
- **Independence:** metrics are reported per log and per independent incident
  (logs from one run are one incident). An incident counts as correct only if
  all its logs are, and as a false match if any of them is.
- **What is measured:**
  - false team-incident matches, other incidents' root causes, and wrong known
    solutions;
  - category, unknown rate and packaged-synthetic matches, kept separate from
    the synthetic `circa evaluate` and `circa benchmark` results.
- **Current result:**
  - ground truth: 4 logs from 1 incident, plus run 41331905922 when its log is
    fetched (5 logs, 2 incidents);
  - category correct in 2 of 2 incidents;
  - 0 false team matches, 0 adopted root causes, 0 wrong known solutions.
  - n=2 says nothing about general accuracy.

## Recommended sources for more real cases

1. **CIRCA's own CI:** each future failed run, with its fix commit and passing
   rerun, is a ready case. These are low-effort, but they're all `software` and
   all from one repository.
2. **tenstorrent/tt-metal (public):** 200 failed runs across 35 workflows in
   about the last 2 days. It's the best source for hardware, infrastructure and
   network failures. Only use runs where a fix commit or a maintainer's
   explanation establishes the cause; reruns that simply pass are not
   validation.
3. **Team repositories the reviewer can access:** these add CI diversity. Add
   only sanitized evidence lines; raw logs never go into the knowledge file.
4. **Other public GitHub Actions projects:** a failing run followed by a commit
   that names the CI fix. Prefer distinct root causes, and add a second
   occurrence of a few incidents so retrieval can be measured.

## Reproducing the evaluation

```bash
python evaluation/holdout.py   # real-world, leave-one-run-out, from preserved logs (Phase 24); --json for details
circa evaluate                                    # synthetic and packaged-only classification
circa benchmark                                   # synthetic retrieval
```

When adding a case, append a `Query` to `QUERIES` in `evaluation/holdout.py`
with its log and `OWNER/REPO#RUN`. Ground truth and exclusion follow from
`team/incidents.json`.

## Next steps toward 20 verified incidents

1. Review and verify the three drafts in `team/drafts/`, taking the count to 5.
2. Add sanitized copies of the two GitHub-only logs to `evaluation/logs/`
   before GitHub's 90-day log retention runs out.
3. Resolve the tt-metal record: confirm the tag and SHA conflict from the full
   log, or reject the record.
4. Collect at least 8 tt-metal incidents with established causes, aiming for
   hardware, infrastructure and network cases.
5. Collect at least 5 more from other repositories, including 2–3 recurrences
   of an existing incident, so retrieval metrics can be defined.
6. Re-run the holdout at each milestone (5, 10, 20). Report incident-level
   numbers with intervals, and don't tune rules against these cases.
