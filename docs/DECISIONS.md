# Decisions log
Format: decision, reason, date. Newest first. Spec fixes are proven by a test.

## 2026-10-07 — LLM stack: Ollama only, no Gemini key (user decision)
- General model: Ollama `gemma4:31b-cloud` on the existing server at `localhost:11434` (verified: answers in ~1.5 s, capabilities completion/thinking/tools/vision). Private/raw-ID pages: local `gemma4:latest` (7.5B, vision, runs locally) and deterministic code. Hugging Face models only where a task needs a specialised model (stamp detection, OCR), chosen when the vision-service is built.
- Reason: the user has no Gemini key and already runs Ollama. Cloud inference leaves the machine, so only Presidio-masked text goes to `gemma4:31b-cloud`; this keeps the earlier privacy rule (raw IDs never leave).
- Dev B follow-up: `llm-gateway`/LiteLLM docs still mention Gemini; point the aliases at Ollama (`ollama_chat/gemma4:31b-cloud`). Dev A talks to Ollama's OpenAI-compatible `/v1` through `HOSP_LLM_BASE_URL` so either the gateway or Ollama directly works.
- The shared Ollama server belongs to the machine, not this repo: do not stop, reconfigure or pull models into it without asking.

## 2026-10-07 — Contract middleware strictness found by schemathesis (spec fixes)
- `X-Idempotency-Key` is required (UUID) on mutating calls; missing/non-UUID -> 400 `bad_request`. Reason: 01-01 §3.1 says required but nothing enforced it.
- `X-Request-Id` (`^[A-Za-z0-9._-]{1,64}$`) and `X-Journey-Id` (UUID) are validated when present; present-but-empty is malformed.
- Required signature headers must be present even in presence-only test mode; wrong method/unknown route passes through to the router (405/404).
- OpenAPI: common 400/401/413/429 documented on every protected operation; `X-Contract-Version` pattern limited to supported major `^1\.\d+$`; spec is 3.1 (pydantic emits JSON-Schema 2020).
- `positive_data_acceptance` is excluded from the schemathesis run: models carry business validators (totals reconcile) that a schema generator cannot satisfy.

## 2026-10-06 — Hospital DB spec fixes (each with a test)
- `next_claim_ref()` raced on `CREATE SEQUENCE IF NOT EXISTS` (pg_class unique violation under concurrency). Fix: advisory lock around lazy creation, `SECURITY DEFINER`. Test: 300 concurrent calls unique and gapless. Migration 0014 adds an optional year argument so rollover is testable.
- `hosp_readonly` column-level REVOKE had no effect on a table-level SELECT grant. Fix: revoke table SELECT, grant an explicit column list excluding `phone_enc`/`id_proof_hash`. Test: SELECT of those columns fails.
- Hospital code is `HOSP-0001` (contract pattern `^HOSP-\d{4}$`), not `DEMO-HOSP`.
- Seed users are the 4 realm users (desk1, officer1-2, hadmin) with fixed ids from `hospital/api/seed/users.json`, shared with the Keycloak realm renderer.
- `alembic check` ignores indexes and CHECK constraints (they live in SQL migrations).
- Doc 04/05/06 define their own migration numbers (0012/0014); real numbering follows the repo: 0014 is `claim_ref_year_param`, later migrations continue from 0015.

## 2026-10-06 — Infra
- MinIO `mc` image/binary are no longer published: bootstrap uses the Python SDK (`infra/minio/init.py`), run from the host.
- ClamAV listens on all interfaces (healthcheck resolves `localhost` to IPv6).
- Host ports shifted in local `.env` (5452, 6390, 9010/9011) because other projects use the defaults.

## 2026-10-06 — Product decisions (user)
- Auto-approve when all gates pass and payable <= `T_auto` (start 50,000 INR); one human above; two above `T_four` (5,00,000). Failed gate/flag forces human review. Round-3 escalation stays; no 50%/80% SLA reminders.
- Default required documents: prescription, pharmacy_bill, final_bill (+ procedure_bill when surgery, implant_sticker for implants); bills hospital-stamped; chronological check.
- Localhost demo, not for sale. All data synthetic. Generate tests rather than fixed counts.
