# hospital-crew

LLM-backed jobs behind a small FastAPI service (`make run-crew`, port 8010, localhost only). The crew never writes
the database: it reads context from the hospital API with its own service token (`hospital-crew` client) and posts
results back to the API's internal endpoints, where the API re-validates everything.

| Job | Reads | Posts to | Model |
|---|---|---|---|
| `claim-build` / `claim-repair` | `GET /v1/internal/cases/{id}/build-context` | `/v1/internal/cases/{id}/claim/draft` | none for arithmetic; local model only to categorise lines the keyword table cannot |
| `query-triage` | `GET /v1/internal/queries/{id}` | `.../triage-result` | general (`HOSP_LLM_MODEL`), never lowers a rule-based risk flag |
| `query-draft` | `GET /v1/internal/queries/{id}/context` | `.../draft-result` | general model on identity-free evidence |

Guards: PII regex before every model call (Aadhaar, PAN, e-mail, phone), grounding rules G01-G08 (same numbers as the
API's guard, so a draft that passes here passes there), a deterministic supervisor (payment promises, liability,
medical advice, links, instruction-like text), repair whitelist (`tools/repair_paths.yaml`).
Prompts are versioned files; `CREW_PROMPT_PIN_<NAME>=v1` pins a version.

Tests: `uv run pytest hospital/crew` (no network), `hospital/api/tests/test_crew_integration.py` (real crew + real API,
fake model), `pytest -m llm` (live Ollama, opt-in).
