# Operations runbook — insurer side (symptom → diagnose → fix → verify)

First step always: `python scripts/doctor.py`. Every claim has a `journey_id`/claim_ref; search logs and the audit tail by it (never by patient data).

## R1 Claim stuck in `received`/`verifying`
Diagnose: `GET /v1/cases/{id}/runs`, `/v1/ops/health`; check `jobs` (manual/inline/arq) and, if `INS_ORCHESTRATOR=n8n`, `ops.outbox` rows with `kind='n8n_trigger'` (parked retries).
Fix: `POST /v1/cases/{id}/verification/rerun` (reviewer) or `POST /internal/outbox/kick`. Verify: run status reaches `completed`; audit shows `verification.completed`.

## R2 Hospital shows a different status than the insurer, or callbacks missing
Diagnose: `GET /v1/ops/dead-letters` (admin) and the `outbox_dead_letter` gauge; compare `sequence` in `ops.outbox` with what the hospital applied.
Fix: `POST /v1/ops/dead-letters/{id}` with action `retry` (same idempotency key, attempts reset) or `discard` with a comment. Verify: hospital `GET /v1/hospital-api/claims/{ref}` matches; gauge back to 0.

## R3 LLM slow/failing or answers look degraded
Diagnose: Langfuse (`http://localhost:3200`) → generations by `session_id=claim_ref`; `x-llm-fallback-used`; crew `/metrics` (`crew_degraded_total`, `crew_repair_total`).
Fix: wait for the free-tier quota window, or switch the gateway offline (`make llm-local`); steps degrade to deterministic checks + manual review by design (never block).
Verify: `crew_degraded_total` stops growing; reviewers no longer see the degraded banner.

## R4 Out of memory / swapping
Diagnose: `docker stats`. Fix: `make down`, start only `make up`; keep `OLLAMA_MAX_LOADED_MODELS=1`; stop n8n (inline orchestrator needs none); lower `INS_CREW_MAX_CONCURRENCY`.

## R5 n8n errors or duplicate executions
Bring-up (tested on n8n 1.64.3): `docker run ... -e NODE_FUNCTION_ALLOW_BUILTIN=crypto -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false -e INS_N8N_WEBHOOK_SECRET=... -v insurer/n8n:/n8n -v scripts:/scripts n8nio/n8n:1.64.3`, then `sh /scripts/n8n_import.sh /n8n/flows` **before** `n8n start` (the CLI cannot activate flows while n8n runs). Re-importing deactivates flows, so always run the script in full.
Flows are stateless and idempotent (`run_id` + `Idempotency-Key`). Re-import with `python insurer/n8n/build_flows.py` and the n8n CLI; check `python scripts/check_flows_clean.py`.

## R6 Login loop / 401
Dev mode uses HS256 tokens: `python scripts/make_dev_token.py reviewer` → paste at `/login`. Tokens expire in 1 h; the audience must equal `INS_KEYCLOAK_AUDIENCE`.

## R7 Audit verification failure (incident)
Diagnose: `GET /v1/cases/{id}/audit/verify` returns the first broken `seq` and reason. Do **not** edit rows. Freeze the case (`senior_reviewer` withdraw-task), export the audit rows, and treat as an integrity incident.
Verify after the investigation: nightly `POST /internal/audit/verify-all` reports `broken: []`.

## R8 A wrong amount was calculated
Run `python -m calc_engine explain <case-input.json>` (trace of S0–S12) and compare with `data/synthetic` reference for the same inputs. Fix the **rule row** (draft → dry-run → publish with a second approver) before touching code; dry-run replays stored calc inputs to show the impact.

## R9 Query loop not progressing
Diagnose: `GET /v1/cases/{id}/queries`; status `draft_ready` means a reviewer must send; `open` means waiting for the hospital; round 3 unanswered escalates (see `/v1/escalations`). Fix: send the draft, or resolve the escalation (goes through the normal gate).

## R10 Reset the environment
`make down`, delete the compose volumes (`docker volume rm claims-insurer_insurer_pg …`), `make up && make seed`. tpa-sim: `POST /sim/reset`.

## R11 Disk filling
Largest consumers: `ollama_models` (~9 GB), `qdrant_data`, `minio_data`, `langfuse_pg`. `POST /internal/housekeeping/idempotency-purge` trims ops tables; Langfuse prunes at 30 days.

## R12 Settlement failed or reversed (simulation)
`GET /v1/settlements`; failures retry at 5 and 10 minutes (3 attempts) then create a settlement task. Admin `retry`/`reverse` actions are in the UI **Settlements** page. No real bank is ever contacted: `INS_SETTLEMENT_MODE` must stay `sim`.

## Alerts
`infra/observability/alerts.yml` (optional Prometheus profile) mirrors 05-04 §9; without Prometheus use `scripts/doctor.py` and the `/v1/ops/health` page.

## Backups (dev scope)
`docker exec claims-insurer-insurer-db-1 pg_dump -U postgres insurer > backup.sql` (weekly); Qdrant is rebuildable with `make seed-kb`; Langfuse data is disposable.
