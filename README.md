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
