# Interview Demo

Three short demos of CIRCA's investigation workflow. They use only the
synthetic logs in `examples/` and a throwaway knowledge file in a temporary
directory, so nothing in the repository changes and `team/incidents.json` is
never read or written.

| Demo                     | Log                                       | What it shows                                              | Needs Ollama |
| ------------------------ | ----------------------------------------- | ---------------------------------------------------------- | ------------ |
| A: Known failure         | `examples/demo_disk_full.log`             | Verified history answers it; no LLM call                   | no           |
| B: Unknown failure       | `examples/demo_unknown_failure.log`       | One local LLM call; the hypothesis becomes a pending draft | optional     |
| C: Insufficient evidence | `examples/demo_insufficient_evidence.log` | `unknown`; no LLM call and no invented root cause          | no           |

No GitHub account, `gh` login, network access or private system is needed.

## Prerequisites

- Python 3.12 or 3.13, and Git to clone the repository.
- For demo B only: [Ollama](https://ollama.com) running locally, with a model
  pulled ([Ollama setup](#ollama-setup-demo-b)). Without it, demo B shows the
  deterministic fallback instead.

## Installation

The demo logs are in the repository, not in the PyPI package, so install from
a clone:

```bash
git clone https://github.com/scmbuildrelease/CIRCA.git && cd CIRCA
python3 -m venv .venv && . .venv/bin/activate
python -m pip install .
circa --version
```

Run every command below from the repository root, in the same shell.

```bash
DEMO="$(mktemp -d)"          # a new, empty directory for the throwaway knowledge file
K="$DEMO/demo-team.json"
```

## Demo A: known failure (no LLM)

Record a synthetic incident whose fix was validated, approve it, then
investigate a new occurrence of the same failure:

```bash
circa knowledge add examples/incident_draft.json --knowledge "$K" --yes
circa knowledge verify team-hardware-scratch-disk-001 --reviewer "Demo Reviewer" --knowledge "$K" --yes
circa investigate-log examples/demo_disk_full.log --knowledge "$K"
```

Expected:

```text
Category: hardware
...
Outcome: known_solution
LLM called: no

Known verified solution (matched by keyword score 9 and its recorded evidence):
Incident team-hardware-scratch-disk-001 (verified, synthetic, hardware)
...
Fix validation: validated
```

**Talking points:**

- The match is accepted only because the incident's recorded evidence lines
  appear in this log after timestamps and hostnames are normalized. Keyword
  similarity alone is never presented as a known solution.
- No model is asked, so the answer is fast, free and deterministic.

## Demo B: unknown failure (optional local LLM)

```bash
export CIRCA_LLM_MODEL=qwen2.5:1.5b    # the model tested for this demo; see Ollama setup
circa investigate-log examples/demo_unknown_failure.log --knowledge "$K" --synthetic
circa knowledge list --pending --knowledge "$K"
```

Expected with Ollama running:

```text
Category: unknown
...
Outcome: new_diagnosis
LLM called: yes
...
Draft saved: draft-06a3cf72d814 (pending_review) in .../demo-team.json
```

The model's wording varies. The draft holds only log lines that were actually
observed, its fix is `unvalidated`, and it is never matched until a person
approves it. A suggested step that would weaken a safeguard (here, allowing
unsigned bundles) is left out of the draft and named in a note.

Running the same command again prints `Outcome: existing_record` and
`LLM called: no`: an open draft isn't diagnosed twice.

**Optional: approve it.** A real reviewer would first confirm the cause. The
draft's category came from the LLM, so it must be confirmed or corrected with
`--category`. Validation evidence is required to mark the fix validated:

```bash
circa knowledge verify draft-06a3cf72d814 --reviewer "Demo Reviewer" --knowledge "$K" --yes \
  --category infrastructure \
  --validation-method repeated_runs_passed \
  --validation-evidence "Synthetic example: 3 consecutive packaging runs passed after renewal"
circa investigate-log examples/demo_unknown_failure.log --knowledge "$K"   # now known_solution, LLM called: no
```

If the model's suggested fix was left out of the draft, `verify` refuses with
`has no fix to validate`. Add the reviewer's fix first: export the draft with
`circa knowledge show draft-06a3cf72d814 --knowledge "$K" --json > draft.json`,
add a `fix`, then run `circa knowledge update draft.json --knowledge "$K" --yes`.

**Without Ollama** the same command prints:

```text
Outcome: deterministic_only
LLM called: no
Note: Local LLM unavailable or returned an invalid response (no Ollama server is reachable at http://localhost:11434; ...)
```

No draft is saved, and the analysis is the same as `circa analyze-log`.

## Demo C: insufficient evidence

```bash
circa investigate-log examples/demo_insufficient_evidence.log --knowledge "$K"
```

Expected:

```text
Category: unknown
Confidence: 0.00
Root cause: Insufficient deterministic evidence to identify a root cause.
...
Outcome: no_failure_evidence
LLM called: no
Note: No recognized failure lines in the log; the LLM was not called and no draft was created.
```

The log only has `INFO` and `WARN` lines and an exit status. CIRCA says it
doesn't know instead of guessing, and it doesn't ask a model to guess either.

## Reset

Start a new shell, or point `K` at a new empty directory, and run the demos
again:

```bash
unset CIRCA_LLM_MODEL
DEMO="$(mktemp -d)"; K="$DEMO/demo-team.json"
```

The demos write only `$K`, so `git status` in the clone stays clean. The old
temporary directory can be deleted by hand or left for the system to clean up.

## Ollama setup (demo B)

[README §10](../README.md#10-optional-local-llm-setup-with-ollama) covers
installing Ollama. For the demo:

```bash
ollama pull qwen2.5:1.5b               # about 1 GB; used in the tested demo run
export CIRCA_LLM_MODEL=qwen2.5:1.5b    # CIRCA's default model is llama3.2
circa doctor                           # "Ollama: ... model qwen2.5:1.5b installed"
```

All inference runs on your machine at `http://localhost:11434`
(`CIRCA_OLLAMA_URL`); nothing is sent to a cloud service. Pull the model
before the interview: the first call after a restart loads the model, which can
take several seconds.

## Troubleshooting

| Symptom                                                             | Fix                                                                                           |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `command not found: circa`                                          | Activate the virtual environment: `. .venv/bin/activate`                                      |
| `... (no Ollama server is reachable at http://localhost:11434 ...)` | Start Ollama (`ollama serve`, or the desktop app); demos A and C don't need it                |
| `... (model 'llama3.2' is not installed on the Ollama server ...)`  | `export CIRCA_LLM_MODEL=qwen2.5:1.5b`, or `ollama pull llama3.2`                              |
| `... did not return a valid structured diagnosis`                   | Small models occasionally reply badly; rerun, or try a larger model                           |
| B prints `Outcome: existing_record`                                 | The draft already exists in `$K`; that is the deduplication working. Reset to start over      |
| B prints `deterministic_only` with a `No draft created:` note       | The model's reply wasn't grounded in the log (for example zero confidence); nothing was saved |
| `knowledge verify` asks for `--category`                            | The draft's category came from the LLM; a reviewer must confirm it                            |
| `already exists` or a duplicate error on `knowledge add`            | `$K` is from an earlier run; reset                                                            |
