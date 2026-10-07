# Multi-Agent Claim Processing

Quickstart (one command; needs Docker, `uv` and, for `make demo`, Ollama):

```bash
make demo-check     # prerequisites only
make demo           # everything on the real local model (Ollama gemma4 + nomic-embed-text)
make demo-offline   # the same with no model, about 2-3 minutes
make demo-down      # stop it all
```

CrewAI on its own, no Docker: `make crew-demo` (hospital flows), `make ins-crew-demo` (insurer flows), `make crew-plot`
(flow diagrams). Runbook: `docs/demo/01-RUNBOOK.md`. Full docs: `docs/implementation/00-README-and-workflow.md`.
CrewAI migration and gap analysis: `docs/CREWAI_MIGRATION_PLAN.md`.
