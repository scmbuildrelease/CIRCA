# CIRCA

## Purpose

CIRCA locally analyzes CI failures and identifies likely software, hardware, network, infrastructure, or unknown causes.

## Guidelines

- Python 3.12+; simple, modular architecture.
- Deterministic analysis is the primary authority.
- Use historical incidents as supporting evidence.
- LLM is optional, local-first, and must not replace deterministic analysis.
- Deterministic behavior must work without LLM services.
- Prefer open-source/local AI; keep dependencies minimal.
- Use Typer, Pydantic, and pytest where appropriate.
- Add tests for new functionality; run the full test suite after meaningful changes.
- Keep v0.1 local, modular, and backward-compatible where practical.
- Keep external integrations optional.
- Do not add Jenkins, Perforce, Slack, Kubernetes, cloud services, vector databases, RAG frameworks, agents, or large AI frameworks unless explicitly requested.
- Never expose secrets, credentials, or sensitive CI data.

## Development

- Reuse existing code before adding abstractions or dependencies.
- Keep modules focused and easy to test.
- Avoid unnecessary files, dependencies, and complexity.
- Implement only the requested phase.
