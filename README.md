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
python -m pip wheel --no-deps --wheel-dir dist .
```

## CI quality gate

GitHub Actions runs on pull requests and pushes to `main` with Python 3.12. It
installs the declared development extras, then runs Ruff, the deterministic test
suite, and a wheel build. The standard checks do not require Ollama, embedding
models, external LLM APIs, credentials, or private infrastructure.

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
