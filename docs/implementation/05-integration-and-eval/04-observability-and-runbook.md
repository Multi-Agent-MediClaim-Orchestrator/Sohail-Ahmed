# 05-04 — Observability and Operations Runbook

Status: PROPOSED. Joint. Langfuse is owned by Dev B; logging/metrics conventions are shared.

## 1. Goal
Make every claim traceable end-to-end across 23 containers: one trace id per stage chain, one journey id per claim, structured logs, LLM traces with cost/latency (Langfuse), service metrics, health checks, alerts that matter on a single developer machine, and a runbook for the failures we expect. The measure of success: given only a `claim_ref`, a developer finds what happened, where, and why in under 5 minutes.

## 2. Inputs / Outputs
Inputs: services emitting logs/spans; `traceparent`, `X-Request-Id`, `X-Journey-Id` headers.
Outputs: Langfuse project per system; Prometheus-format `/metrics` per service; optional Grafana dashboards (profile `obs`); `ops/runbook.md` (generated from section 11 of this doc); `make doctor`/`make logs` tooling; `ops/drills.md`; alert evaluator.

## 3. Identifiers (PROPOSED)
| Id | Format | Scope |
|---|---|---|
| `trace_id` | W3C `traceparent` 32-hex | one pipeline stage chain; propagated through HTTP, n8n, crews |
| `span_id` | 16-hex | one operation |
| `case_id` | UUIDv7 | per system (hospital and insurer have different values) |
| `claim_ref` / `insurer_claim_no` | `HC-YYYY-NNNNNN` / `IC-YYYY-NNNNNN` | cross-system correlation |
| `journey_id` | `J-` + claim_ref | spans both systems; hospital sends it in header `X-Journey-Id` (additive contract header, minor bump to 1.1; see 3.2) |
| `run_id` | UUIDv7 | one agent crew run |
| `request_id` | UUID | one HTTP request |
Every log line, audit event, Langfuse trace and n8n execution note carries `case_id` and `trace_id`; cross-system records also carry `journey_id`.

### 3.1 Which id answers which question
| Question | Use |
|---|---|
| "What happened to this claim across both sides?" | `journey_id` |
| "Why did this one LLM answer look wrong?" | Langfuse trace by `run_id`, linked from `trace_id` |
| "Why did this HTTP call fail?" | `request_id` in the access log of both caller and callee |
| "Is the audit trail intact?" | `case_id` + `verify_chain` |

### 3.2 Contract impact (needs approval of both devs)
Add optional request header `X-Journey-Id` to `01-api-contract-v1.md` section 2 and bump `contract_version` to 1.1 (additive). Receivers must echo it in logs and responses; absence is not an error (fallback: derive `J-<claim_ref>`).

## 4. Logging

### 4.1 Format
JSON lines to stdout:
```json
{"ts":"2026-10-06T10:15:30.123Z","level":"INFO","svc":"hospital-api","env":"dev","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7",
 "journey_id":"J-HC-2026-000123","case_id":"0192f0b2-...","actor":"user:desk-3","event":"doc.uploaded","msg":"document stored",
 "attrs":{"doc_type":"final_bill","size":183422,"sha256_8":"a1b2c3d4"},"dur_ms":41}
```

### 4.2 Field dictionary
| Field | Required | Notes |
|---|---|---|
| `ts` | yes | UTC ISO-8601 ms |
| `level` | yes | DEBUG/INFO/WARN/ERROR |
| `svc` | yes | container name |
| `env` | yes | dev/integ |
| `trace_id`, `span_id` | yes when in request/job context | |
| `journey_id`, `case_id` | when known | |
| `actor` | yes | `user:<id>`, `agent:<name>`, `system:<job>`, `peer:<key_id>` |
| `event` | yes | dotted event name, same vocabulary as audit event types (01-04 section 7) where one exists |
| `msg` | yes | short human text, no PII |
| `attrs` | no | flat key/values; hashes and sizes only |
| `dur_ms` | on completion events | |
| `err` | on ERROR | `{type, message, stack_hash}`; full stack goes to a separate `stack` field only at DEBUG |

### 4.3 Python implementation (`structlog`)
```python
# contract/python/claim_contract/observability/logging.py
import structlog, logging, sys
from claim_contract.audit import redact


def _redact(_, __, event_dict):
    event_dict["attrs"] = redact(event_dict.get("attrs", {}))
    event_dict["msg"] = redact({"m": event_dict.get("event_msg", "")})["m"]
    return event_dict


def configure(svc: str, env: str = "dev", level: str = "INFO"):
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # trace_id, journey_id, case_id, actor
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            lambda l, n, d: {**d, "svc": svc, "env": env},
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
```
Middleware sets contextvars per request: `trace_id` from `traceparent` (or new), `journey_id` from header, `request_id`. A job runner sets them from the message envelope.

### 4.4 Node/UI and n8n
- Next.js servers: `pino` with the same field names; browser errors are posted to `POST /v1/client-log` (rate limited, redacted) and logged by the API.
- n8n: a first Code node on every workflow (`_common/context`) sets `$execution.customData` keys `case_id`, `trace_id`, `journey_id`; every HTTP Request node uses the shared header sub-workflow so the ids propagate. n8n execution list is then filterable by case id.

### 4.5 Levels and rules
DEBUG off by default. INFO: state transitions, external calls (peer, gateway, MinIO), job start/finish. WARN: retries, LLM provider fallback, low-confidence routes, config cache miss. ERROR: operation failed and a case is affected. Never log request or response bodies; log sha256 prefix (8 chars) and sizes. A CI test greps all logs from a full S25 run for C2/C3 patterns (see `03-privacy-and-security.md`).

### 4.6 Event vocabulary used in logs (beyond audit events)
`http.request`, `http.response`, `peer.call`, `peer.retry`, `outbox.enqueued`, `outbox.delivered`, `outbox.dead_letter`, `llm.call`, `llm.fallback`, `llm.privacy_block`, `parser.job.start`, `parser.job.done`, `queue.enqueue`, `queue.dequeue`, `config.resolved`, `reconcile.mismatch`, `auth.denied`.

### 4.7 Aggregation and querying
Default: `docker compose logs` piped through `jq`. `make logs SVC=hospital-api CASE=<id>` runs:
```bash
docker compose logs --no-log-prefix -t $SVC 2>/dev/null | jq -c "select(.case_id==\"$CASE\" or .journey_id==\"$CASE\")"
```
`make journey ID=J-HC-2026-000123` runs the same filter over all services and sorts by `ts`, printing `ts svc level event msg`. Optional profile `obs` adds Loki + Promtail with a Grafana Explore view.

## 5. Tracing

### 5.1 OpenTelemetry
Libraries: `opentelemetry-instrumentation-fastapi`, `-httpx`, `-sqlalchemy`, `-redis`. Export OTLP to an optional `otel-collector` (profile `obs`) feeding Jaeger. PROPOSED default: tracing export off (no-op) to save RAM; Langfuse covers LLM-level traces and logs carry ids. Set `OTEL_SDK_DISABLED=true` unless `obs` profile is up.

```python
def setup_tracing(svc: str):
    if os.getenv("OTEL_SDK_DISABLED", "true") == "true":
        return
    provider = TracerProvider(resource=Resource.create({"service.name": svc}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]))
    )
    trace.set_tracer_provider(provider)
```

### 5.2 Span naming
`<svc>.<operation>`: `hospital-api.completeness.evaluate`, `doc-pipeline.parse`, `insurer-crew.agent.identity`, `calc-engine.compute`.
Required span attributes: `case_id`, `journey_id`, `config.version.<domain>` where applicable.

### 5.3 Propagation matrix
| Hop | Mechanism |
|---|---|
| browser → API | `traceparent` generated by UI fetch wrapper |
| API → API/services | HTTP headers `traceparent`, `X-Journey-Id`, `X-Request-Id` |
| API → n8n | webhook body `context` + headers; n8n forwards via `_common/headers` |
| n8n → crew | request body `context: {trace_id, journey_id, case_id, run_id}` |
| crew → gateway | headers + LiteLLM `metadata` (`trace_id`, `session_id=journey_id`, `trace_name`) |
| API → Redis queue | message envelope `{ids, payload_ref}` |
| hospital ↔ insurer | headers only (contract 1.1); each side starts a new local trace linked by `journey_id` |

### 5.4 Cross-system rule
Traces are not shared between the two systems (isolation principle). Only `journey_id` and `claim_ref` cross the boundary; never internal `case_id`s.

## 6. LLM observability (Langfuse)

### 6.1 Setup
Self-hosted Langfuse plus its Postgres. PROPOSED: pin a v2 image (no ClickHouse) to save RAM; revisit if v3 is required. LiteLLM callbacks `success_callback: ["langfuse"]`, `failure_callback: ["langfuse"]` so every call is traced automatically (architecture rule: all calls go through the gateway). Two Langfuse projects `hospital` and `insurer` selected through LiteLLM virtual keys (`hospital-crew-key`, `insurer-crew-key`), which also give per-system budget and rate limits.

LiteLLM config excerpt:
```yaml
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  cache: true
  cache_params: {type: redis, ttl: 3600}
  num_retries: 2
  request_timeout: 60
  fallbacks:
    - {"fast-llm": ["fast-llm-lite", "local-llm"]}
    - {"strong-llm": ["fast-llm", "local-llm"]}
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
model_list:
  - {model_name: fast-llm, litellm_params: {model: gemini/gemini-flash, api_key: os.environ/GEMINI_API_KEY, rpm: 10}}
  - {model_name: fast-llm-lite, litellm_params: {model: gemini/gemini-flash-lite, api_key: os.environ/GEMINI_API_KEY, rpm: 15}}
  - {model_name: local-llm, litellm_params: {model: ollama/llama3.2:3b, api_base: http://ollama:11434}}
```
(Model names/limits are placeholders; quotas change, so read them from env at build time.)

### 6.2 Trace shape
```
trace (name: "claim:<journey_id>:<stage>", session_id=journey_id, user_id=actor, tags=[system, stage])
 ├─ span agent:<AgentName>   (run_id, crew version, prompt_version)
 │   ├─ generation llm_call  (alias, provider, tokens, cost, latency, cache_hit)
 │   └─ span tool:<name>     (inputs hash, outputs summary)
 └─ scores: schema_valid, deterministic_check_passed, human_edit_distance, human_accepted
```
`session_id = journey_id` groups all stages of a claim for a system. UI claim detail pages deep-link to the Langfuse trace (role-gated; Reviewer/Officer/Admin only).

### 6.3 Prompt management
Prompts stored in Langfuse with labels `dev`, `prod`; code fetches by `name` + `label`, caches 60 s, falls back to a committed copy in `prompts/` when Langfuse is unreachable (log WARN `prompt.fallback`). Each audit event `model_info.prompt_version` records the version used. Change workflow: edit `prompts/*.md` → PR → CI runs eval-fast for changed prompts → on merge `make prompts-sync` pushes to Langfuse with label `prod`.

Prompt file naming: `prompts/<system>/<agent>/<purpose>.md` with front matter `{name, version, variables[], output_schema}`.

### 6.4 Scores
Automatic: `schema_valid`, `retry_count`, `grounded_numbers_ok` (every number in a draft appears in the source), `privacy_block`. Human: normalised edit distance between agent draft and final sent text → `human_edit_distance`; approvals → `human_accepted` 1/0. These feed the evaluation report in `02-evaluation-harness.md`.

### 6.5 Cost and latency
Free tier: track calls/min vs provider limit, token counts, fallback rate, cache hit rate. Budget alarm at 80% of daily quota (alerts, section 9). Per-claim cost view: sum generations by `session_id`.

## 7. Metrics (Prometheus format, `/metrics` on every service)

| Metric | Type | Labels |
|---|---|---|
| `http_requests_total`, `http_request_duration_seconds` | counter/hist | svc, route, status |
| `claim_stage_duration_seconds` | hist | system, stage |
| `claims_in_status` | gauge | system, status |
| `queue_depth` | gauge | queue (parse, n8n, outbox, vision) |
| `outbox_pending`, `outbox_dead_letter` | gauge | direction |
| `callback_delivery_attempts_total` | counter | direction, outcome |
| `llm_calls_total`, `llm_fallback_total`, `llm_tokens_total` | counter | alias, system |
| `privacy_block_total` | counter | system, category |
| `human_queue_age_seconds` | gauge | queue (review, approval, query) |
| `audit_chain_verify_failures_total` | counter | system |
| `doc_parse_duration_seconds`, `doc_parse_low_confidence_total` | hist/counter | doc_type |
| `reconciliation_mismatch_total` | counter | system |
| `config_cache_miss_total` | counter | domain |

Cardinality rule: never label with `case_id`, `claim_ref` or user id.

### 7.1 Stage names for `claim_stage_duration_seconds`
Hospital: `scan, parse, classify, completeness, build, signoff, submit`. Insurer: `receive, identity, authenticity, coverage, calculation, decision_wait, query_wait, settlement`.

### 7.2 Dashboards (profile `obs`, provisioned JSON in `ops/dashboards/`)
1. Pipeline funnel: counts per status per system; p50/p95 stage durations.
2. Human queues: review/approval/query queue size and oldest age vs SLA.
3. LLM health: calls/min vs limit, fallback %, cache hit %, latency by alias, cost.
4. Contract delivery: outbox sizes, delivery attempts by outcome, dead letters.
5. Resources: container CPU/RAM (cAdvisor), disk.

## 8. Health checks and readiness
Every service exposes `/v1/health` (liveness, always cheap) and `/v1/ready` (checks dependencies with 2 s timeouts, returns per-dependency status). Compose `healthcheck` calls `/v1/health`; `depends_on: condition: service_healthy` sets startup order.

```json
GET /v1/ready -> 200
{"status":"ready","version":"0.3.1","deps":{"db":"ok","redis":"ok","minio":"ok","gateway":"degraded"},"config":{"doc_requirements":4,"deadlines":2}}
```
Readiness is `degraded` (still 200) if only an optional dependency is down (gateway), `not_ready` (503) if a required one is.

### 8.1 `make doctor` output (golden file `tests/golden/doctor.txt`)
```
SERVICE         UP  READY   VERSION  CFG        QUEUE  NOTE
hospital-api    ok  ready   0.3.1    dr4/dl2    0
insurer-api     ok  ready   0.3.0    pr3/th2    0
doc-pipeline    ok  ready   0.2.4    -          2
llm-gateway     ok  degraded 0.1.8   -          -      quota 74%
...
AUDIT last verify: hospital ok 02:00, insurer ok 02:00
OUTBOX dead-letter: hospital 0, insurer 0
DISK free: 41%    RAM used: 12.1/16 GB
```

## 9. Alerts (PROPOSED)
Local setting: alerts surface as a banner in each Admin UI (`GET /admin/alerts`) and in `make doctor`; optional webhook (ntfy/Slack) via `ALERT_WEBHOOK_URL`.

### 9.1 Rules
| Alert | Condition | Severity | First action |
|---|---|---|---|
| OutboxDeadLetter | `outbox_dead_letter > 0` | high | R2 |
| CallbackFailing | 5 consecutive delivery failures to one peer | high | R2 |
| AuditChainBroken | `audit_chain_verify_failures_total` increases | critical | R7 |
| PrivacyBlockC3 | `privacy_block_total{category="C3"}` increases | critical | check `03-privacy-and-security.md` incident steps |
| LLMFallbackHigh | fallback rate > 30% for 10 min | medium | R3 |
| LLMQuotaNearLimit | daily usage > 80% | medium | R3 |
| HumanQueueStale | oldest review item > configured SLA | medium | notify role |
| DiskLow | free < 10% | high | R4 |
| ContainerFlapping | > 3 restarts / 10 min | high | R4 |
| ParserBacklog | > 20 docs waiting > 5 min | medium | R1 |
| ReconcileMismatch | `reconciliation_mismatch_total` increases | high | R2/R9 |

### 9.2 Prometheus rule examples (for profile `obs`)
```yaml
groups:
- name: claims
  rules:
  - alert: OutboxDeadLetter
    expr: outbox_dead_letter > 0
    for: 1m
    labels: {severity: high}
  - alert: LLMFallbackHigh
    expr: rate(llm_fallback_total[10m]) / rate(llm_calls_total[10m]) > 0.3
    for: 10m
    labels: {severity: medium}
  - alert: AuditChainBroken
    expr: increase(audit_chain_verify_failures_total[1h]) > 0
    labels: {severity: critical}
```

### 9.3 Alert evaluator (no-Prometheus mode)
A small Python job (`ops/alert_eval.py`, run every 60 s by a compose `restart: always` sidecar in each system) scrapes its own `/metrics`, evaluates the same rules via a table, writes active alerts to `alert` table (`id, rule, since, severity, detail, acknowledged_by`). Admin UI banner reads `GET /admin/alerts`.

## 10. Failure-mode catalogue (what to expect)
| Failure | Visible symptom | Runbook |
|---|---|---|
| ClamAV not ready (first 1-2 min after start) | uploads wait `scan_pending` | R1 |
| Parser crash/timeouts | doc stays `parsing` | R1 |
| Peer unreachable / HMAC mismatch | outbox grows, claim `submitting` | R2 |
| LLM quota/429 | fallback rate rises; slower | R3 |
| RAM exhaustion | containers restart | R4 |
| n8n queue stall | cases idle between stages | R5 |
| Keycloak misconfig | login loops, 401 | R6 |
| DB tamper/bug | audit alert | R7 |
| Calc doubts | disputes of amounts | R8 |
| Query timers broken | rounds do not advance | R9 |
| Corrupt dev state | anything weird | R10 |

## 11. Runbook (symptom → diagnose → fix → verify)

### R1 Claim stuck in `docs_pending` although documents were uploaded
Diagnose:
1. `make logs SVC=hospital-api CASE=<id>` and look for `doc.scanned` verdict per document. If missing, ClamAV is down or still loading signatures (`docker compose ps clamav`; `docker compose logs clamav | tail`; allow 1-2 minutes after start).
2. Look for `doc.parsed`. If missing, check `queue_depth{queue="parse"}` and `docker compose logs doc-pipeline`.
3. Look at the `completeness.evaluated` event `missing[]`. It may be correct: a conditional document required by the config (e.g. implant sticker). Compare with `doc_requirements` version recorded.
4. Check parse confidence: low-confidence documents are held for human review and are not "missing".
Fix:
- ClamAV: wait or `docker compose restart clamav`. Parser: `docker compose restart doc-pipeline`.
- Re-run completeness: `POST /cases/{id}/reevaluate` (idempotent).
Verify: status moves to `docs_complete`; audit has a new `completeness.evaluated` event.

### R2 Hospital shows `submitted` but insurer has no claim (or vice versa)
Diagnose:
1. Query outbox: `GET /admin/outbox?state=pending|dead` on the sending side; read `last_error`.
   - 401 `invalid_signature`: secrets differ. Run `make check-secrets` (compares first 8 hex chars of SHA-256 of each secret, never the secret).
   - 401 `stale_request`: clock skew. `docker exec <c> date -u` on both containers vs host `date -u`.
   - 409 `idempotency_conflict`: body changed between attempts; do not edit queued bodies, regenerate the submission as a new attempt.
   - 424/5xx: peer unhealthy: `make ping-peers`.
2. Confirm the receiving side's access log for the `request_id`.
Fix: correct config, then `make outbox-retry ID=<outbox_id>` (reuses the same idempotency key, so duplicates are impossible).
Verify: `GET /claims/{ref}` on the insurer returns `received`; hospital moves to `acknowledged`; outbox dead-letter gauge returns to 0.

### R3 LLM calls failing or slow
Diagnose:
1. Langfuse: filter traces with errors in the last hour; check LiteLLM `GET /health/liveliness` and `GET /health` for model status.
2. HTTP 429 means free-tier quota: the gateway should fall back to the next alias. If `local-llm` is not running: `docker compose --profile llm up -d ollama`.
3. Model missing: `docker exec ollama ollama pull llama3.2:3b`.
Fix: reduce concurrency (`LITELLM_MAX_PARALLEL`), enable cache, switch `LLM_MODE=local-only` during provider outages.
Verify: pipelines must degrade to "human review required" with reason `llm_unavailable`, never hang. Confirm cases moved to that state, and that `llm_fallback_total` stops growing.

### R4 Out of memory / host swapping (16 GB machine)
1. `docker stats --no-stream | sort -k4 -h` to list offenders; `free -h`; `dmesg | grep -i oom`.
2. Reduce footprint in order: stop `obs` profile; set parser concurrency 1; use 3B Ollama model; run one system at a time (`make up-hospital` or `make up-insurer`); stop `vision-service` if not needed.
3. Check `mem_limit` values in compose: the offending container should be killed instead of the host.
Verify: no restarts for 10 minutes; `make doctor` all ready.

### R5 n8n workflow errors or duplicates
1. n8n UI → Executions → filter by the custom data `case_id`.
2. Workflows are idempotent via keys derived from `case_id + step`; re-running a failed execution is safe.
3. Stalled worker: `docker exec redis redis-cli llen bull:jobs:wait` and `...:active`; restart the worker container.
4. If a workflow JSON changed: re-import (`make n8n-import`), do not edit in the UI without exporting afterwards (`make n8n-export`).
Verify: case advances; no duplicate audit events for the same step.

### R6 Keycloak login loop / 401
Checklist: container clock skew; realm import stale (`make keycloak-reimport`); redirect URI mismatch after changing a port; token audience mismatch between UI client and API; `KC_HOSTNAME` mismatch between browser URL and issuer URL in API config (very common).

### R7 Audit verification failure (incident)
1. Stop writes for the case: set `audit_frozen=true` (admin endpoint).
2. `python -m claim_contract.audit verify --case <id>` prints the first broken `seq`.
3. Compare with the daily anchor in `audit_anchor` and MinIO object-locked file.
4. In dev, usually a manual DB edit or a bug in canonical JSON (decimal or ordering change). Restore from backup, or if a bug, fix and re-chain only with a documented incident (never silently).
5. Record under `ops/incidents/YYYY-MM-DD-<id>.md`.

### R8 Wrong calculation reported
1. Open `calc_trace_id` in the insurer UI (shows inputs, rule ids, intermediate values).
2. `POST /calc/replay` with stored inputs and the stored policy version; compare with `reference_calc` from the synthetic ground truth.
3. Add the case to golden tests first, then fix, then re-run the S01-S16 suite.

### R9 Query loop not progressing
Check: `query_policy` version; round counter on the query row; n8n wait node state; tpa-sim scripted scenario state; `make ping-peers` for callback reachability; query SLA timers (`due_by`). Round 4 attempts must fail with 409; if they do not, that is a defect (S1).

### R10 Resetting the environment
`make reset-data` (drop DB volumes, reseed synthetic data, reimport Keycloak realms, flush Redis, re-create MinIO buckets). `make backup` / `make restore TS=<timestamp>`.

### R11 Disk filling up
`docker system df`; prune images (`docker image prune`), Ollama models you do not use, old MinIO objects in `kb-sources` test uploads, Langfuse DB growth (set retention 14 days). Alert DiskLow triggers this.

### R12 Document stuck in vision/quality step
Check vision-service logs for ONNX load errors; confirm model files mounted; run `make vision-selftest` (processes a golden image and prints outputs).

## 12. Backup and DR (dev scope)
`make backup` writes `backups/<ts>/` containing: `pg_dump` of both DBs and Langfuse DB, MinIO mirror (`mc mirror`), n8n workflow export, Keycloak realm export, `.env` fingerprints (not secrets). `make restore` reverses it and runs `make doctor`. Restore test is part of acceptance.

## 13. Build tasks
1. Shared `observability` package in `contract/python/claim_contract/observability/` (structlog config, OTel setup, context vars, metrics registry, redaction hook).
2. FastAPI middleware shared by all services: request id, traceparent/journey parsing, access log, `/metrics`, `/v1/health`, `/v1/ready`.
3. n8n `_common/context` and `_common/headers` sub-workflows and execution-note convention.
4. LiteLLM config: Langfuse callbacks, virtual keys per system, budget/rate limits, fallback chain.
5. Langfuse deploy with persistent volume; `make langfuse-init` creates projects and keys, writes them to `.env.local`.
6. Prompt sync scripts (`prompts/` ↔ Langfuse) and loader with committed fallback.
7. Score emitters (schema_valid, grounded_numbers_ok, human_edit_distance).
8. `obs` profile: collector, Prometheus, Grafana provisioned dashboards (optional).
9. Make targets: `doctor`, `logs`, `journey`, `backup`, `restore`, `outbox-retry`, `ping-peers`, `check-secrets`, `vision-selftest`.
10. Alert evaluator and `alert` table plus `GET /admin/alerts`.
11. Add `X-Journey-Id` to the contract (needs both approvals) and update `01-api-contract-v1.md`.
12. Write `ops/runbook.md` from section 11 and create `ops/drills.md` template.

## 14. Tests
- Log redaction: feed PII corpus, assert no match in output.
- Context propagation: a request through hospital-api → n8n → crew → gateway carries one `trace_id`; log lines from each contain it.
- Journey continuity: S01 run; `make journey` shows ordered logs from at least 8 services; one Langfuse session per system.
- Health/ready: stop Redis; `/v1/ready` returns 503 with `redis: down`; liveness stays 200.
- Prompt loader fallback with Langfuse stopped.
- Outbox dead-letter alert fires within 2 minutes of injection.
- `make doctor` golden output on a healthy stack.
- Backup/restore round trip leaves audit chains verifiable.
- Metrics cardinality test: scrape after 200 cases; assert no label has more than 50 distinct values.

## 15. Acceptance criteria
- For any S01 run, searching a `journey_id` shows ordered logs from ≥ 8 services and one Langfuse session per system with all LLM calls.
- LLM provider outage simulation falls back and still reaches `ready_for_review` or an explicit human-review state with reason.
- Each runbook entry R1-R10 reproduced once in a drill (inject fault, follow steps, recover) and logged in `ops/drills.md` with date and tester.
- Alerts in 9.1 each fired at least once in a drill.
- No C2/C3 pattern in any log from the S25 run.

### 15.1 Drill template (`ops/drills.md`)
```
## R2 — peer unreachable (2026-xx-xx, tester A)
Inject: docker stop insurer-api
Observed: outbox_pending=1, claim status submitting (retrying)
Followed steps: 1,2 -> fix: docker start insurer-api
Recovery time: 3m10s
Gaps found in runbook: <list> -> PR #
```

## 16. Dependencies and Claude Code kickoff prompt
Depends on: 01-04 (audit/redact), 01-01 (headers), llm-gateway doc, all service docs for middleware adoption. Dev B: tasks 4-7, 11 (as contract editor with A's approval); Dev A: tasks 1-3, 9; joint: 8, 10, 12.

> Implement docs/implementation/05-integration-and-eval/04-observability-and-runbook.md tasks I own (state A or B). Start with the shared observability package or the LiteLLM/Langfuse wiring respectively, add the tests in section 14, and verify with a trace across two services.
