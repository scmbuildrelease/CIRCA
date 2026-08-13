# CIRCA

## Purpose

CIRCA (Continuous Integration Root Cause Analyzer) locally analyzes CI failures and identifies likely software, hardware, network, infrastructure, or unknown causes.

## Guidelines

- Use Python 3.12+ and a simple modular architecture.
- Prefer deterministic analysis first, supported by historical incident matching.
- Keep dependencies minimal; use Typer, Pydantic, and pytest.
- Add tests for all new functionality.
- Keep v0.1 local.
- Do not add Jenkins, Perforce, Slack, Kubernetes, cloud services, vector databases, embeddings, or LLM APIs yet.
