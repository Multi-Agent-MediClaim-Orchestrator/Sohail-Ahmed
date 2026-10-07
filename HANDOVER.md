# Dev A (hospital side) — handover

Everything assigned to Dev A is built, tested and committed locally (never pushed). `docs/DECISIONS.md` is the
authoritative log of every deviation from the specs; this file is the short version plus what is left.

## What exists
| Area | Where | Notes |
|---|---|---|
| Shared contract | `contract/` (joint) | models, enums, state machines, HMAC signing, idempotency, audit chain, middleware, OpenAPI, insurer/hospital simulators. Changes were additive; contract tests green. |
| Infra | `infra/` | Postgres (hospital), Redis ACLs, MinIO (Python init, no `mc`), ClamAV, Keycloak (two realms), n8n compose. |
| Hospital DB | `hospital/api/alembic` | migrations 0001-0022, models generated from the migrated schema. |
| hospital-api | `hospital/api` | auth/RBAC, cases, documents, completeness, router, claim builder + submission outbox, callbacks, query inbox, SSE, dashboard, metrics, internal endpoints. `openapi.json` committed. |
| n8n flows | `hospital/n8n` | 13 generated workflows, linted, tested against a real n8n container. |
| crew | `hospital/crew` | claim build/repair, query triage and grounded drafts over Ollama. |
| UI | `hospital/ui` | Next.js 14: desk, officer, admin screens. |
| doc-pipeline | `services/doc-pipeline` | parse, classify, mask, two-pass extraction. |
| vision-service | `services/vision-service` | page quality, stamps, registry match. |
| Synthetic data + eval | `data/synthetic`, `data/eval` | hospital-side corpus generator and scoring harness. |
| E2E | `scripts/e2e_hospital.py` | `make e2e-hospital`. |

## Run it
`make init && make up-infra && make migrate && make seed && make up-n8n`, then `make run-api`, `make run-crew`,
`make run-docpipe`, `make run-vision`, `make run-ui` (ports 8100, 8010, 8200, 8300, 3100; other projects on this
machine hold 8000, 3000, 5432, 6379, 9000). Demo users: desk1, officer1, officer2, hadmin (password `DEMO_PW`).
`scripts/get_token.sh <user>` and `scripts/verify_auth.sh` help with auth checks.
Tests: `make test` (contract, infra, hospital, crew, doc-pipeline, vision, synthetic, eval), `make n8n-test` (docker),
`make test-llm` (live Ollama), `make eval-hospital` after `make seed-data`.

## Decisions you made that are implemented
Ollama only (`gemma4:31b-cloud` general, `gemma4:latest` local/private); no Gemini or provider keys; only
Presidio-masked text may reach the cloud model and identity documents never do; stamps required on bills;
default required documents prescription + pharmacy bill + final bill (+ procedure bill for surgery, implant sticker for
implants); no 50%/80% SLA reminders (overdue at 100% only); round-3 query escalation kept; two approvers for round 3 /
risky query categories; all data synthetic.

## Deviations worth knowing (details in DECISIONS.md)
- CrewAI package not used (plain agents, same HTTP surface); no LLM `classify-extract`/`supervise` jobs.
- doc-pipeline: MinerU is an opt-in backend (`DOCPIPE_PARSER=mineru`, local `mineru-kit`; measured 7x slower than tesseract, no classification gain); text layer + tesseract by
  default; values are unmasked locally before reaching hospital-api; no `/v1/unmask`; jobs in memory.
- vision-service: learned stamp detector (random forest on candidate regions, numpy inference, trained on synthetic stamps only; held-out P/R 0.99/0.97 vs classical 0.74/0.56 on the same set), tesseract OCR, local-model escalation.
- n8n: thin flows over what the API already does; one process on the host network; in-app reminders only (no SMTP).
- UI: npm, cookie-session proxy with password grant against the dev Keycloak client (not PKCE); has audit explorer with chain
  verification and a page-image document viewer; no saved views or version diff.
- Test-harness fixes: no background outbox workers in tests (they raced the manual runs), small DB pools, crew PII
  guard ignores UUIDs. Postgres `max_connections` is now 100; `make test`/`make e2e-*` queue through `scripts/run_exclusive.sh`.

## Verification status (2026-10-07)
- `make e2e-hospital` (deterministic extractors, about 20 s): passes, 11 steps from case creation to settlement and audit,
  against the real stack and the insurer simulator.
- `E2E_LLM=ollama make e2e-hospital` (real local `gemma4:latest`, thinking off, cloud off): passed once in about 7 minutes
  (parsing 72 s, query draft about 5 minutes). An earlier real-model run failed once at the draft step with an API 422 whose
  cause was not captured (job errors now carry the response body); it did not recur. The real-model runs also found and
  fixed five defects (see DECISIONS.md). Do not run the test suites while the real-model e2e is running: contention
  made one suite run fail 88 tests (not reproducible alone).
- Suites: ruff clean, mypy (contract) clean, contract+infra+hospital 461 passed repeatedly, doc-pipeline, vision-service,
  data (synthetic + eval) and 13 n8n flow tests (real n8n container) green; `openapi.json` current; doc-column check clean.

## Known gaps / not verified
- UI is now browser-tested (`make ui-e2e`: 10 Playwright tests in system Chrome, axe serious/critical zero), but only on Chrome and synthetic data.
- Stamp detector and MinerU numbers are synthetic-only; real scans are untested.
- Insurer side: only `synth/reference_calc.py` (hand-checked) and `evalh/tune_t_auto.py` exist. Dev B must supply decision
  rows (`payable`, `gates_pass`, `correct`) from the real engine to tune `T_auto` (stays 50,000 INR, target false-approve
  below 1%); archetypes S11-S16/S21/S22/S26 and tpa-sim scripts S19/S20 are not built.
- Dockerfiles exist for every service but only `hospital-api` was build-tested; the demo runs on the host.
- Keycloak password-grant client and `.env` secrets are for localhost only.

## For Dev B / for you
- llm-gateway docs (04-shared-services/04) still describe Gemini; the insurer UI event names differ from doc 04.
- Fill in: nothing is required. `HF_TOKEN` in `.env` is optional (only for a Hugging Face model). There is no Gemini key.
- Local Ollama must have `gemma4:latest`; the `*-cloud` model needs Ollama's own sign-in and sends masked text off the
  machine. `DOCPIPE_ALLOW_CLOUD=false` keeps everything local (the e2e run does this).
