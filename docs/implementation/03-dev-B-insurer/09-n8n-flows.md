# 03-09 — insurer-n8n: Workflow Flows (2.5)

Owner: Dev B. Status: PROPOSED. Container `insurer-n8n` (n8n CE queue mode + worker, port 5679).
Depends on: docs 03, 04, 05, 06 internal endpoints; 08 crew endpoints; Redis (queue); Keycloak service account.

## 1. Goal
Use n8n as the orchestration and timer layer: sequence verification steps, react to human actions, call crews and the API, and handle retries. n8n holds **no business rules** and no state beyond execution data. Every decision, validation and persistence happens in insurer-api. The database is the source of truth; flows are stateless and re-entrant.

Design tenets:
1. **Stateless, re-entrant flows.** A flow can be killed at any node and restarted; insurer-api dedupes by `(run_id, step)` and by `Idempotency-Key`.
2. **No long waits inside n8n.** Human waits (approval, query response) and multi-day timers are NOT `Wait` nodes. They are modelled as DB rows + cron sweeps + new webhook triggers.
3. **No PII in n8n.** Flows pass IDs and small structured results; context payloads are fetched by the step and not persisted in execution data.
4. **One entry per event.** Each webhook corresponds to exactly one API event type; the API retries delivery with the same `client_token`.

## 2. Inputs / Outputs

### 2.1 Inputs
| Source | Mechanism | Event |
|---|---|---|
| insurer-api outbox | HTTP webhook `POST /webhook/<name>` with `X-N8N-Signature` | `verification-start`, `decision-task-open`, `query-sent`, `query-response`, `escalation-raised`, `settlement-initiate`, `decision-final` |
| n8n Cron trigger | every 5 min, nightly 02:00 IST | SLA and housekeeping |
| Redis pub/sub | (optional, via API only) | none consumed directly by n8n in v1 |

### 2.2 Outputs
- Calls to insurer-api `/internal/*` (service account JWT).
- Calls to insurer-crew `/v1/*` (no auth beyond internal network + service token header `X-Service-Token`).
- Alerts: `POST /internal/alerts` and Redis `PUBLISH alert:n8n`.
- Emitted domain events: `POST /internal/events` (API republishes to the SSE stream `/v1/events/stream`).

### 2.3 Webhook payload contracts
All webhooks: JSON, max 16 MB (`N8N_PAYLOAD_SIZE_MAX=16`), header `X-N8N-Signature: sha256=<hex hmac of raw body with INS_N8N_WEBHOOK_SECRET>`, header `X-Event-Id` (uuid), `X-Client-Token`.

| Webhook path | Body |
|---|---|
| `verification-start` | `{ "case_id": "uuid", "trigger": "claim_received|query_response|manual_rerun|config_change", "steps": ["all"] \| ["identity","coverage",...], "client_token": "uuid" }` |
| `decision-task-open` | `{ "case_id", "task_id", "tier": "single|dual", "amount": "123456.00", "sla_due_at": "ISO", "recommended_outcome": "approve|partial|reject" }` |
| `query-sent` | `{ "case_id", "query_id", "round": 1, "due_by": "ISO" }` |
| `query-response` | `{ "case_id", "query_id", "response_id" }` |
| `escalation-raised` | `{ "case_id", "escalation_id", "reason": "round3_unresolved|calc_conflict|integrity|manual" }` |
| `settlement-initiate` | `{ "case_id", "decision_id", "payable": "..." }` |
| `decision-final` | `{ "case_id", "decision_id", "outcome" }` |

## 3. Data model

### 3.1 Flow inventory (exported JSON in `insurer/n8n/flows/`)
Flows are committed to git; credentials referenced by name only (never exported with values).

| File | Type | Trigger | Summary |
|---|---|---|---|
| `00_common_error_handler.json` | main | Error Trigger | writes alert, marks run failed via API |
| `01_get_token.json` | sub | Execute Workflow | Keycloak client-credentials token, cached 4 min |
| `02_call_api.json` | sub | Execute Workflow | HTTP wrapper with retry, idempotency key, problem+json mapping |
| `03_call_crew.json` | sub | Execute Workflow | HTTP wrapper with 100 s timeout, 429 `busy` backoff |
| `04_emit_event.json` | sub | Execute Workflow | publishes domain event via API |
| `10_verification_main.json` | main | Webhook `verification-start` | whole pipeline |
| `11_step_document_fetch.json` | sub | Execute Workflow | polls fetch status |
| `12_step_completeness.json` | sub | Execute Workflow | API-only (deterministic) |
| `13_step_identity.json` | sub | Execute Workflow | crew + API |
| `14_step_authenticity.json` | sub | Execute Workflow | crew + API |
| `15_step_coverage.json` | sub | Execute Workflow | crew + API |
| `16_step_calculation.json` | sub | Execute Workflow | mapper crew → calc → API |
| `20_decision_gate.json` | main | Webhook `decision-task-open` | notify approvers, no waiting |
| `21_decision_final.json` | main | Webhook `decision-final` | triggers outbound callbacks and settlement |
| `30_query_sent.json` | main | Webhook `query-sent` | registers timers (via API), notifies |
| `31_query_response.json` | main | Webhook `query-response` | triage then optional re-verify |
| `40_escalation.json` | main | Webhook `escalation-raised` | notify senior reviewers, SLA |
| `50_settlement.json` | main | Webhook `settlement-initiate` | initiate and poll tpa-sim |
| `60_cron_sla_and_housekeeping.json` | main | Cron 5 min + nightly | sweeps |

### 3.2 n8n storage
Postgres database `n8n_insurer` on the insurer-db instance, own role `n8n_insurer` (no access to `insurer` schema). Workflow static data used only for the token cache.

### 3.3 Idempotency
`Idempotency-Key = sha1(execution_id + ":" + node_name + ":" + attempt_group)` where `attempt_group` is stable across retries of the same node (so retries dedupe, restarts of the whole execution produce a different execution id but the API also dedupes `(run_id, step)`).

## 4. API/endpoints consumed

### 4.1 insurer-api internal endpoints (service JWT, role `n8n-service`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/internal/cases/{id}/runs` | create/lookup run; body `{trigger, steps, client_token}` → `{run_id, config_versions, steps_to_run, existing: bool}`; `409 run_in_progress` if another run is active |
| GET | `/internal/cases/{id}/docs-status` | `{all_fetched: bool, failed:[...], pending:n}` |
| POST | `/internal/runs/{run_id}/steps/{step}/start` | marks step running, returns `{attempt}` |
| GET | `/internal/cases/{id}/context?for={step}` | step input bundle (masked) |
| POST | `/internal/runs/{run_id}/steps/{step}/result` | store output → `{next_step, run_status}` |
| POST | `/internal/runs/{run_id}/calc` | API builds `CalcInput`, calls calc-engine, stores result |
| POST | `/internal/runs/{run_id}/finalize` | `{next: "needs_info|ready_for_decision|failed|manual_review"}` |
| POST | `/internal/cases/{id}/queries/draft` | asks API to ask crew-draft via n8n result (see 6.1) |
| POST | `/internal/queries/{id}/triage-result` | `{rerun: bool, steps: [...]}` |
| GET | `/internal/queries/due?within=PT10M` | reminders and timeouts to process |
| POST | `/internal/queries/{id}/reminder` | record reminder sent |
| POST | `/internal/cases/{id}/round-timeout` | round timeout reached |
| GET | `/internal/tasks/due` | decision tasks needing SLA nudges |
| POST | `/internal/tasks/{id}/nudge` | record nudge |
| GET | `/internal/cases/stuck?older_than=PT20M` | stuck runs |
| POST | `/internal/runs/{id}/restart` | rescue |
| POST | `/internal/outbox/kick` | force outbox dispatcher poll |
| POST | `/internal/audit/verify-all` | nightly chain verification |
| POST | `/internal/settlement/{case_id}/initiate` | start settlement |
| GET | `/internal/settlement/{case_id}/status` | poll |
| POST | `/internal/alerts` | raise alert |
| POST | `/internal/events` | emit domain event |
| POST | `/internal/housekeeping/idempotency-purge` | delete old idempotency rows |

### 4.2 Crew endpoints (doc 08)
`/v1/identity/verify`, `/v1/authenticity/check`, `/v1/coverage/assess`, `/v1/calc/map-lines`, `/v1/query/draft`, `/v1/query/triage`, `/v1/supervisor/summarise`. Doc 05 now uses the same names (reconciled).

### 4.3 Webhook authentication
First nodes of every webhook flow:
1. **Webhook** node, `Raw Body` on, `Response: Using 'Respond to Webhook' Node`.
2. **Function (Verify signature)**:
```js
const crypto = require('crypto');
const raw = $input.first().binary?.data ? Buffer.from($input.first().binary.data.data, 'base64') : Buffer.from(JSON.stringify($json.body));
const sig = ($input.first().json.headers['x-n8n-signature']||'').replace('sha256=','');
const exp = crypto.createHmac('sha256', $env.INS_N8N_WEBHOOK_SECRET).update(raw).digest('hex');
const ok = sig.length===exp.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(exp));
return [{json:{ok, body: $json.body, event_id: $json.headers['x-event-id']}}];
```
3. **IF** `ok` false → **Respond to Webhook** 401 `{code:"invalid_signature"}` and stop.
4. **Respond to Webhook** 202 `{accepted:true}` immediately, then continue (so API outbox treats it as delivered; processing is asynchronous). PROPOSED: respond 202 before processing, since processing is idempotent and recoverable by cron rescue.

## 5. Build tasks
1. **Compose.** Service `insurer-n8n` (main) and `insurer-n8n-worker` with `EXECUTIONS_MODE=queue`, `QUEUE_BULL_REDIS_HOST=redis`, `QUEUE_BULL_REDIS_DB=2`, Postgres `DB_TYPE=postgresdb`, `N8N_ENCRYPTION_KEY`, `WEBHOOK_URL=http://insurer-n8n:5678/`, `N8N_DIAGNOSTICS_ENABLED=false`, `N8N_VERSION_NOTIFICATIONS_ENABLED=false`, `N8N_PERSONALIZATION_ENABLED=false`, worker `command: worker --concurrency=5`. `mem_limit: 1g` each. Files: `infra/compose/insurer.yml`, `insurer/n8n/Dockerfile` (pins n8n version), `insurer/n8n/.env.example`.
2. **Credentials.** Create via CLI import `insurer/n8n/credentials/*.template.json` (values from env at bootstrap script `scripts/n8n_bootstrap.sh`): `insurer-keycloak-client` (OAuth2 client credentials), `insurer-webhook-secret` (env only), `insurer-crew-service-token`.
3. **Sub-workflow `01_get_token`.** Nodes: Function (check static data `token, exp`) → IF valid → return; else HTTP Request POST Keycloak token URL (`grant_type=client_credentials`) → Function (store `token`, `exp = now + expires_in - 60`) → return `{token}`.
4. **Sub-workflow `02_call_api`.** Inputs `{method, path, body, idempotency_key, expect:[200,202]}`. Nodes: Execute `get_token` → HTTP Request (timeout 30 s, `Continue On Fail`) → Function (classify: 2xx ok; 409 `run_in_progress` → ok flag `already:true`; 4xx → hard error; 5xx/timeout → retry) → Loop with Wait (2 s, 8 s, 30 s; max 3) → return `{status, body}`.
5. **Sub-workflow `03_call_crew`.** Inputs `{path, body}`. HTTP timeout 100 s. On 429 with `Retry-After`, wait that long (cap 60 s, max 3). On 422 `invalid_output`, return `{ok:false, reason:"invalid_output"}` to caller (caller decides retry). On 5xx/timeouts return `{ok:false, reason:"unavailable"}`.
6. **Sub-workflow `04_emit_event`.** POST `/internal/events` `{type, case_id, data}`.
7. **Build `10_verification_main`** per section 6.1. Idempotent on `case_id + client_token`.
8. **Build step sub-workflows** `11`-`16` per sections 6.2-6.7.
9. **Parallel identity + authenticity.** In `10_verification_main`, use two **Execute Workflow** nodes fed from one node (n8n runs branches sequentially per execution; to get real parallelism set `Execute Workflow` option "Wait for Sub-Workflow Completion" off for both, then poll run status). PROPOSED: simplest reliable approach — run sequentially (identity then authenticity) in v1 and revisit if verification time > target. Document this decision in the flow's sticky note. A feature flag `PARALLEL_STEPS` in the API's `steps_to_run` response may later split them.
10. **Decision gate flows** `20`, `21` per sections 6.8-6.9.
11. **Query flows** `30`, `31` per sections 6.10-6.11.
12. **Escalation flow** `40` per section 6.12.
13. **Settlement flow** `50` per section 6.13.
14. **Cron flow** `60` per section 6.14.
15. **Error handler** `00_common_error_handler`, assigned in each workflow's Settings → Error Workflow (script sets it on import).
16. **Scripts.** `scripts/n8n_export.sh` (`n8n export:workflow --all --separate --output=insurer/n8n/flows --pretty`; strips `credentials` ids and `pinData`), `scripts/n8n_import.sh` (`n8n import:workflow --separate --input=...` then activate list from `insurer/n8n/active_flows.txt`), `scripts/n8n_drain.sh` (waits for running executions to finish).
17. **Execution data policy.** `EXECUTIONS_DATA_PRUNE=true`, `EXECUTIONS_DATA_MAX_AGE=168`, `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none`, `EXECUTIONS_DATA_SAVE_ON_ERROR=all` but error paths pass through a Set node that drops `context`/`body` fields before the error is raised.
18. **CI hygiene check** `scripts/check_flows_clean.py`: parse all flow JSON; fail on any `credentials` entry containing non-empty `data`, URLs with `token=`/`key=`, strings matching JWT regex, or hard-coded hostnames outside an allowlist.
19. **Test harness** (section 9): `tests/n8n/` pytest + wiremock stubs, Newman collection.
20. **Docs inside flows.** Sticky note per flow: purpose, trigger, inputs, outputs, failure policy; node naming `<Verb> <Object>` (e.g. `POST run create`), layout left→right, error branches below main path.

## 6. Key flows (node by node)

### 6.1 `10_verification_main`
| # | Node | Type | Config / logic |
|---|---|---|---|
| 1 | Webhook verification-start | Webhook | POST `verification-start`, raw body |
| 2 | Verify signature | Function | see 4.3 |
| 3 | IF signature ok | IF | else Respond 401 |
| 4 | Respond 202 | Respond to Webhook | `{accepted:true}` |
| 5 | Create run | Execute `02_call_api` | `POST /internal/cases/{case_id}/runs` body `{trigger, steps, client_token}` |
| 6 | Run exists? | IF | `already == true` → **Exit (success)** (duplicate delivery) |
| 7 | Set run vars | Set | `run_id`, `config_versions`, `steps_to_run` |
| 8 | Step: document_fetch | Execute `11_step_document_fetch` | only if `steps_to_run` contains `document_fetch` |
| 9 | Step: completeness | Execute `12_step_completeness` | |
| 10 | Completeness blocking? | IF | `result.next_step == "finalize"` → jump to 17 |
| 11 | Step: identity | Execute `13_step_identity` | |
| 12 | Step: authenticity | Execute `14_step_authenticity` | |
| 13 | Identity hard fail? | IF | API returns `next_step=="finalize"` → 17 |
| 14 | Step: coverage | Execute `15_step_coverage` | |
| 15 | Step: calculation | Execute `16_step_calculation` | |
| 16 | (reserved) Supervisor summary | Execute call_crew `/v1/supervisor/summarise` | non-blocking; failure ignored |
| 17 | Finalize run | Execute `02_call_api` | `POST /internal/runs/{run_id}/finalize` |
| 18 | Switch on `next` | Switch | `needs_info` → 19; `ready_for_decision` → 20 (then call `POST /internal/cases/{id}/decision/auto-decide`: the API either auto-approves, actor=system, or opens the approval task for a human — see 04 §6.2a); `manual_review` → 21; `failed` → 22 |
| 19 | Request query draft | Execute `03_call_crew` `/v1/query/draft` (input from `GET context?for=query_draft`) → `POST /internal/cases/{id}/queries/draft` with crew output | API validates and stores draft; reviewer sends later (human in loop) |
| 20 | Open decision task | `POST /internal/events` `decision.ready` (API creates `decision_task` at finalize; this node only notifies) | |
| 21 | Notify manual review | `POST /internal/events` `case.manual_review` | |
| 22 | Raise alert | `POST /internal/alerts` severity `high` | |
| 23 | End | NoOp | |

Notes:
- Nodes 8-15 each check the step list so partial reruns (`steps: ["identity","coverage"]`) work; skipped steps are no-ops.
- The API, not n8n, decides skip logic (`next_step` in every step result). n8n only follows it.

### 6.2 `11_step_document_fetch`
1. `POST .../steps/document_fetch/start`.
2. Loop (max 30 iterations, wait 10 s): `GET /internal/cases/{id}/docs-status`.
3. Exit when `all_fetched`; if iteration cap reached → post result `status=failed, finding=doc.fetch_timeout`.
4. If `failed` list non-empty → post result with `findings=[doc.unavailable...]` (API sets `needs_info` with fixable flag since hospital can resend URL).
5. Post result `status=passed`.
Edge: expired presigned URLs → API handles refresh via hospital callback; n8n only polls.

### 6.3 `12_step_completeness`
`start` → `GET context?for=completeness` is unnecessary (API computes) → `POST .../steps/completeness/result` with empty body and `compute:true`; API runs deterministic checks and returns `{next_step, run_status, findings_count}`. No crew call.

### 6.4 `13_step_identity`
1. `start`.
2. `GET context?for=identity` → `{claim_identity, policy_member, id_docs:[{doc_id, extracted}], deterministic_scores}`; deterministic matching score is computed by the API **before** the crew; the crew only explains mismatches/fuzzy cases.
3. IF `deterministic_scores.decisive` (clear match or clear fail) → skip crew.
4. Else Execute `03_call_crew` `/v1/identity/verify` (retry up to 2 on `invalid_output`, waits 10 s, 30 s; on `unavailable` fall to failure policy 6.15).
5. `POST .../result` `{agent_output, trace_id, crew_attempts}`; API revalidates and decides.

### 6.5 `14_step_authenticity`
Same shape as 6.4 with `GET context?for=authenticity` (vision-service signals, stamp checks, arithmetic checks, metadata anomalies all computed by API/shared services); crew `/v1/authenticity/check` only drafts explanations of flagged items; the gate uses deterministic scores only.

### 6.6 `15_step_coverage`
`GET context?for=coverage` (policy snapshot version, waiting periods, exclusions, retrieved clauses with citation ids from rag-service done by the crew). Crew `/v1/coverage/assess` returns `{clause_ids, applies, rationale}`; API validates that every cited clause id exists in the policy snapshot (no free-text-only claims) and rejects otherwise (n8n retries once with `retry_reason`).

### 6.7 `16_step_calculation`
1. `start`.
2. `GET context?for=calc_mapping` → bill lines, policy rule keys.
3. Crew `/v1/calc/map-lines` → `{line_id → category, rule_keys, flags}` (mapping only; no arithmetic).
4. `POST /internal/runs/{run_id}/calc` with the mapping; API builds `CalcInput`, calls calc-engine, stores `calc_result` + `trace`, returns `{total_payable, deltas_vs_claimed}`.
5. `POST .../steps/calculation/result` `{status, calc_result_id}`.
Math is never done by the crew or n8n.

### 6.8 `20_decision_gate`
Trigger `decision-task-open`. Nodes: verify signature → respond 202 → emit event `approval.requested` (so UI toasts via SSE) → if `tier=="dual"` emit additional `approval.requested_second` placeholder (API decides who) → optional SMTP node (MailHog in dev) to approver distribution list from `GET /internal/roles/approvers/emails` → end. **No Wait node.** SLA nudges are done by cron (6.14) calling `/internal/tasks/due`.

### 6.9 `21_decision_final`
Trigger `decision-final`. Nodes: verify → respond 202 → `POST /internal/cases/{id}/callbacks/decision` (API enqueues outbound callback to hospital via outbox, see doc 06) → switch on outcome: `approve|partial` → `POST /internal/settlement/{case_id}/initiate` (or trigger `50_settlement` by webhook) ; `reject` → end.

### 6.10 `30_query_sent`
Trigger `query-sent`. Purpose: registers nothing locally. API already stores `due_by`, reminder offsets from `query_policy`. This flow only emits `query.sent` event and optionally mail. Timers are in cron (6.14).

### 6.11 `31_query_response`
1. Trigger `query-response {case_id, query_id, response_id}`; verify; respond 202.
2. `GET context?for=query_triage` → `{query, response_text, attached_docs_summary, requirements, open_findings}`.
3. Crew `/v1/query/triage` → `{addressed:[...], unaddressed:[...], new_issues:[...], suggested_status}`.
4. `POST /internal/queries/{id}/triage-result` → API applies deterministic rules (e.g. required doc types present?) and returns `{rerun: bool, steps: [...], round_action: "close|next_round|escalate"}`.
5. IF `rerun` → Execute main flow logic via HTTP POST to own webhook `verification-start` (trigger `query_response`, steps from API). Using webhook (not Execute Workflow) keeps the run in its own execution.
6. Emit event `query.triaged`.
7. If `round_action == "escalate"` → API itself raises escalation (emits `escalation-raised`).

### 6.12 `40_escalation`
Trigger `escalation-raised`. Verify → respond 202 → `GET /internal/escalations/{id}/pack` (API assembles) → emit `escalation.opened` → mail senior reviewers → end. SLA tracking: cron checks `/internal/tasks/due` (includes escalations).

### 6.13 `50_settlement`
Trigger `settlement-initiate`. Nodes: verify → respond 202 → `POST /internal/settlement/{case_id}/initiate` (API calls tpa-sim) → Loop (max 12, wait 30 s) `GET /internal/settlement/{case_id}/status` until `settled|failed` → on failed emit alert and leave to cron retry. Settlement callbacks from tpa-sim land on API directly, so polling is a fallback; if the status turns `settled` the loop exits early.

### 6.14 `60_cron_sla_and_housekeeping`
**Every 5 minutes** (sequence of HTTP nodes, each `Continue On Fail`):
1. `GET /internal/queries/due?within=PT10M` → for each item: `type=reminder` → `POST /internal/queries/{id}/reminder`; `type=round_timeout` → `POST /internal/cases/{id}/round-timeout` (API decides next round or escalation).
2. `GET /internal/tasks/due` → for each: `POST /internal/tasks/{id}/nudge` (emits `sla.nudge` / `sla.breached`).
3. `GET /internal/cases/stuck?older_than=PT20M` → for each: `POST /internal/runs/{id}/restart` (max 2 restarts, then alert).
4. `POST /internal/outbox/kick`.
**Nightly 02:00 IST:** `POST /internal/audit/verify-all`; `POST /internal/audit/anchor`; `POST /internal/config/cache-refresh`; `POST /internal/housekeeping/idempotency-purge`.
Concurrency guard: API takes an advisory lock per sweep type so overlapping executions are harmless.

### 6.15 Failure policy
- **Crew `unavailable` or timeouts:** 2 retries (wait 10 s, 30 s). Then post step result `{status:"failed", finding:"step.agent_unavailable"}`; API sets degraded manual review (step can be done by human reviewer), run continues to finalize.
- **Crew `invalid_output`:** up to 2 retries with `retry_reason` string appended to input; after that same as unavailable but finding `step.agent_invalid_output`.
- **API 5xx:** `02_call_api` retries 3x (2 s, 8 s, 30 s). Then Error flow runs: `POST /internal/alerts`, Redis `alert:n8n`; case stays `verifying`; cron rescue restarts after 20 min.
- **API 4xx (not 409 run_in_progress):** no retry; error flow; alert `medium`.
- **Idempotency:** header on every call.

### 6.16 `00_common_error_handler`
Nodes: Error Trigger → Function (build `{workflow, node, message, execution_id}`, strip any `context`/`body`) → `POST /internal/alerts` → IF the failed workflow was `10_verification_main` and `run_id` known → `POST /internal/runs/{run_id}/fail` (API sets `needs_attention`) → Redis publish.

## 7. Config / env vars
| Var | Purpose |
|---|---|
| `N8N_ENCRYPTION_KEY` | credential encryption (secret) |
| `N8N_HOST`, `N8N_PORT=5678`, `WEBHOOK_URL=http://insurer-n8n:5678/` | |
| `EXECUTIONS_MODE=queue`, `QUEUE_BULL_REDIS_HOST/PORT/DB` | queue |
| `DB_TYPE=postgresdb`, `DB_POSTGRESDB_HOST/DATABASE=n8n_insurer/USER/PASSWORD` | state |
| `EXECUTIONS_DATA_PRUNE=true`, `EXECUTIONS_DATA_MAX_AGE=168`, `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` | PII hygiene |
| `INS_API_URL=http://insurer-api:8100`, `INS_CREW_URL=http://insurer-crew:8110` | targets |
| `INS_KEYCLOAK_TOKEN_URL`, `INS_N8N_CLIENT_ID`, `INS_N8N_CLIENT_SECRET` | service token |
| `INS_N8N_WEBHOOK_SECRET` | HMAC for inbound webhooks |
| `INS_CREW_SERVICE_TOKEN` | crew auth |
| `N8N_PAYLOAD_SIZE_MAX=16` | |
| `GENERIC_TIMEZONE=Asia/Kolkata` | cron |
| `N8N_RUNNERS_ENABLED=true` | task runners |
| `NODE_FUNCTION_ALLOW_BUILTIN=crypto` | allow crypto in Function nodes |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | needed for `$env` in signature node (PROPOSED; restrict by only mounting required vars) |
Memory: main 1 GB, worker 1 GB.

## 8. Error handling and edge cases
1. **Duplicate webhook delivery** (API outbox retry): `client_token` makes `POST /runs` idempotent; node 6 exits quietly when `already:true`.
2. **n8n restart mid-run:** queue mode re-queues the job; the step API is idempotent per `(run, step, attempt)`; cron rescue handles lost executions (>20 min stale).
3. **Worker crash during crew call:** crew may complete work but n8n repeats the call; crews are stateless and cache by input hash in LiteLLM, so cost is limited; API accepts only the first `result` per `(run, step, attempt)`.
4. **Concurrent runs for one case:** API returns `409 run_in_progress`; second trigger is dropped if `trigger` is not `query_response`, otherwise API queues a follow-up run (`pending_rerun=true`) and starts it at finalize.
5. **Config published mid-run:** run keeps `config_versions` from creation; a `config_change` rerun is only started by a human action.
6. **Sensitive data:** never log bodies; Set node drops `context` before any error output; node option "Always Output Data" off; execution data saved only on error and masked.
7. **Version drift:** CI imports exported flows into a throwaway n8n container and runs the smoke scenario.
8. **Time zones:** cron in IST; payload timestamps UTC ISO-8601.
9. **Endpoint name drift:** doc 05 names crew endpoints `/draft-query`, `/triage-response`; this doc and doc 08 use `/v1/query/draft` and `/v1/query/triage`. Resolved: doc 08 names are canonical and doc 05 has been updated. Implementation keeps the paths in a single n8n environment-driven constant set in `03_call_crew` mapping table to ease the change.
10. **Webhook exposure:** n8n on the internal Docker network only; insurer-ui never calls n8n; webhook signature required.
11. **Changing an in-use flow:** deactivate → drain (`n8n_drain.sh`) → import → activate; never edit in prod UI (UI edits are overwritten at next import; export script detects drift and fails CI).
12. **Large fan-out:** a nightly sweep with many items uses `Split In Batches` (size 20) to cap memory.
13. **Clock skew:** HMAC has no timestamp window in v1 (event id dedupes); API includes `X-Event-Id` and n8n rejects repeats via API `409`.
14. **Token expiry mid-flow:** `01_get_token` caches until 60 s before expiry; `02_call_api` re-fetches once on 401.
15. **Crew busy (429):** honoured with `Retry-After`; counts against run time budget (30 min); beyond that the run finishes as degraded.
16. **Poison events:** after 8 failed API deliveries to n8n, API parks the outbox row in dead letters (visible in `/admin/ops`); cron rescue can re-drive.

## 9. Tests

### 9.1 Harness
- `tests/n8n/conftest.py`: starts compose subset (`n8n`, `n8n-worker`, `redis`, `postgres`, `wiremock` for crew, real or stubbed insurer-api).
- `tests/n8n/test_flows.py` (pytest): sends signed webhooks via helper `sign(body)`; polls API stub state for expected calls.
- `tests/n8n/newman/*.postman.json` for manual smoke.

### 9.2 Scenario matrix
| # | Scenario | Setup | Expected |
|---|---|---|---|
| T1 | Happy path | all steps pass, calc ok, payable ≤ `T_auto` | steps called in order; finalize `ready_for_decision`; `auto-decide` auto-approves (`decision.auto_approved`); no human task |
| T2 | Fixable blocker | completeness finds missing doc | finalize `needs_info`; query draft call made; crew draft invoked once |
| T3 | Unfixable blocker | identity hard fail | coverage/calc skipped; finalize per API; no draft |
| T4 | Crew invalid output ×3 | wiremock returns 422 | step result `failed` with `agent_invalid_output`; run finalizes degraded |
| T5 | Crew down | connection refused | retries 2x; failure result; no infinite loop |
| T6 | API 500 transient | stub 500 twice then 200 | retry succeeds; single step result recorded |
| T7 | API 500 persistent | always 500 | error flow; alert posted; case unchanged |
| T8 | Duplicate webhook | same `client_token` twice | second exits; one run only |
| T9 | n8n worker killed mid-step | kill worker at step 3 | cron rescue restarts; final state correct, no duplicate result |
| T10 | Bad signature | wrong HMAC | 401; no API calls |
| T11 | Query response → rerun | triage says rerun | new verification-start with trigger `query_response` |
| T12 | Query response → escalate | triage says escalate | no rerun; escalation flow triggered by API event |
| T13 | Cron: reminder due | `queries/due` returns reminder | `POST reminder` called once |
| T14 | Cron: round timeout | returns timeout | `round-timeout` called |
| T15 | Cron: stuck run | `cases/stuck` returns 1 | `restart` called; at third time alert |
| T16 | Settlement poll | status `pending` ×3 then `settled` | loop exits, no alert |
| T17 | Settlement failed | status `failed` | alert and retry by cron |
| T18 | Document fetch timeout | docs-status never complete | result `doc.fetch_timeout` after 5 min (accelerated clock in test) |
| T19 | Decision gate dual tier | tier=dual | `approval.requested` emitted once |
| T20 | Error handler | force exception in Function node | alert + run fail call, no PII in alert body |

### 9.3 Hygiene and performance
- `check_flows_clean.py` passes; DB inspection test: after T1-T20, `execution_data` contains no strings from the PII corpus (names, member ids, phone).
- Performance: 20 simultaneous verifications with stubbed crew (0.5 s latency) finish < 10 min on a 16 GB dev machine, worker memory < 1 GB.

## 10. Acceptance criteria
- [ ] Imported flows run a happy-path claim end to end against the real insurer-api with stubbed crew in < 60 s.
- [ ] T1-T20 pass in CI.
- [ ] Idempotent re-delivery proven; no duplicate steps, queries or decisions.
- [ ] No PII in n8n execution data (DB inspection test).
- [ ] Stuck-run rescue works (kill worker mid-run test).
- [ ] Flow JSON exports reproducible via script and pass hygiene check; flows have sticky notes.
- [ ] No Wait node longer than 60 s anywhere (lint check over flow JSON).

## 11. Dependencies
- Docs 03 (run/step internal APIs), 04 (decision tasks), 05 (queries, timeouts, escalation), 06 (settlement), 08 (crew endpoints).
- Redis, Keycloak (Dev A infra; `insurer` realm client `n8n-service`), insurer-db instance for n8n state.
- `04-shared-services/06-tpa-sim.md` for settlement polling behaviour.
- MailHog optional (dev).

## 12. Claude Code kickoff prompt
> Read docs/implementation/03-dev-B-insurer/09-n8n-flows.md and the internal endpoint sections of docs 03-06 in the same folder. Plan first. Implement tasks 1-20: compose config, token and wrapper sub-flows, step sub-flows, main flow, decision/query/escalation/settlement flows, cron flow, scripts and tests. Work against stubbed crew and stubbed API first (wiremock), then against the real API. Do not put business rules in n8n. Verify with scenarios T1-T20 and report against section 10.
