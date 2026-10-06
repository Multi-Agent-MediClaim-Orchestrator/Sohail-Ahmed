# 04-06 — tpa-sim (simulated insurer/TPA behaviour)

Owner: **Dev B**. Port 8500 (FastAPI plus a small UI). Status: PROPOSED where marked.

Related docs: `01-shared-contract/01-api-contract-v1.md` (the contract tpa-sim implements), `01-shared-contract/02-data-models-and-enums.md` (models, state machines), `02-dev-A-hospital/06-api-claim-builder-submission.md` and `07-api-query-inbox-sse.md` (consumers of tpa-sim), `03-dev-B-insurer/05-api-query-loop-escalation.md` (real behaviour tpa-sim imitates), `05-integration-and-eval/05-end-to-end-integration-plan.md` (scenarios used in milestones).

---

## 1. Goal

Let Dev A develop and test the entire hospital system **before** the real insurer system exists, and later use the same simulator for scripted demos, resilience tests and evaluation. tpa-sim implements the insurer side of the versioned contract with scripted, deterministic scenarios: acknowledgement, status updates, multi-round queries, decisions, settlements and deliberate faults.

Mirror tool for Dev B: a `hospital-sim` fixture (section 9.6) that behaves like the hospital (submits signed claims, answers query callbacks) so the insurer system can be built without the hospital system.

Design rules for tpa-sim:
1. **Scripted, not smart.** It contains no verification or calculation logic. Outcomes come from scenario files. Duplicating insurer-api logic would create two sources of truth.
2. **Contract-strict.** It validates inbound requests exactly as the real insurer-api will (signature, timestamp, idempotency, schema, totals), so hospital bugs surface early.
3. **Deterministic by default.** Same input + same scenario + same time scale = same event sequence. LLM text mode is optional and off by default.
4. **Fault injectable.** Delays, drops, duplicates, reordering, bad signatures and clock skew can be triggered per claim or globally to test the contract's delivery guarantees (contract §8).
5. **Observable.** Everything it receives and sends is visible in its UI and logs.

---

## 2. Inputs / Outputs

### 2.1 Inputs
- Signed requests from hospital-api to the insurer-facing endpoints (contract §5.1): claim submissions, status polls, supplementary documents, query responses, withdrawals.
- Control-plane requests from developers/tests (`/sim/*`).
- Scenario YAML files (`services/tpa-sim/scenarios/*.yaml`) and query templates.
- Presigned document URLs inside submissions (downloaded optionally to verify `sha256`).

### 2.2 Outputs
- Synchronous: `202 Acknowledgement`, `200 StatusUpdate`, errors in RFC 7807.
- Asynchronous callbacks to hospital-api `/v1/insurer-callbacks/{status|queries|decisions|settlements}` signed with the insurer→hospital HMAC secret, each carrying `sequence`.
- UI and `/sim/*` JSON describing state; structured logs; Prometheus metrics (`tpasim_callbacks_total{kind,result}`).

### 2.3 Environment wiring (dev)
```
hospital-api: INSURER_BASE_URL=http://tpa-sim:8500  (real system later: http://insurer-api:8100)
tpa-sim:      HOSP_BASE_URL=http://hospital-api:8000
secrets:      HOSP_TO_INS_HMAC_SECRET (verify inbound), INS_TO_HOSP_HMAC_SECRET (sign outbound)
```

---

## 3. Data model

tpa-sim has its **own** SQLite (default) or Postgres schema; it never touches insurer-db.

```sql
CREATE TABLE sim_claim (
  claim_ref        TEXT PRIMARY KEY,
  insurer_claim_no TEXT UNIQUE NOT NULL,
  scenario_id      TEXT NOT NULL,
  state            TEXT NOT NULL,            -- InsurerCaseStatus value (01-02) simulated
  round            INT  NOT NULL DEFAULT 0,  -- current query round 0..3
  sequence         INT  NOT NULL DEFAULT 0,  -- last callback sequence sent
  step_cursor      INT  NOT NULL DEFAULT 0,  -- index of next scenario step
  received_at      TIMESTAMPTZ NOT NULL,
  idem_key         TEXT NOT NULL,            -- idempotency key of the submission
  payload_hash     TEXT NOT NULL,
  payload          JSONB NOT NULL,           -- the ClaimSubmission as received
  override_json    JSONB,                    -- per-claim overrides (delays, ratios)
  chaos_json       JSONB,                    -- per-claim chaos settings
  withdrawn        BOOLEAN NOT NULL DEFAULT FALSE,
  closed           BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE sim_event (                      -- scheduled callbacks
  id          INTEGER PRIMARY KEY,
  claim_ref   TEXT NOT NULL REFERENCES sim_claim,
  kind        TEXT NOT NULL,                  -- status|query|decision|settlement|probe
  due_at      TIMESTAMPTZ NOT NULL,
  fired_at    TIMESTAMPTZ,
  attempts    INT NOT NULL DEFAULT 0,
  result      TEXT,                           -- ok|failed|dropped|duplicated
  payload     JSONB NOT NULL,
  seq_assigned INT,
  depends_on_response BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ix_sim_event_due ON sim_event (due_at) WHERE fired_at IS NULL;

CREATE TABLE sim_query (
  query_id     TEXT PRIMARY KEY,
  claim_ref    TEXT NOT NULL REFERENCES sim_claim,
  round        INT NOT NULL,
  category     TEXT NOT NULL,
  text         TEXT NOT NULL,
  requested_doc_types JSONB,
  due_by       TIMESTAMPTZ,
  status       TEXT NOT NULL,                 -- open|answered|closed|escalated
  response_json JSONB,
  answered_at  TIMESTAMPTZ
);

CREATE TABLE sim_idempotency (
  key_id TEXT, idem_key TEXT, request_hash TEXT, status INT, body JSONB, created_at TIMESTAMPTZ,
  PRIMARY KEY (key_id, idem_key)
);

CREATE TABLE sim_log (                         -- every inbound/outbound message
  id INTEGER PRIMARY KEY, ts TIMESTAMPTZ, direction TEXT,      -- in|out
  claim_ref TEXT, method TEXT, path TEXT, status INT,
  sequence INT, signature_ok BOOLEAN, idem_replay BOOLEAN, chaos TEXT,
  request_body JSONB, response_body JSONB, error TEXT
);

CREATE TABLE scenario (                        -- loaded from YAML; stored for UI and uploads
  id TEXT PRIMARY KEY, name TEXT, description TEXT, steps JSONB, match JSONB, source TEXT
);
```

Insurer claim numbers: `IC-{year}-{6-digit seq}` where seq is a counter table (`sim_counter`), matching contract format in 01-05.

---

## 4. API

### 4.1 Insurer-facing endpoints (identical to contract §5.1)

| Method | Path | Behaviour in tpa-sim |
|---|---|---|
| POST | `/v1/hospital-api/claims` | verify → idempotency → validate → pick scenario → persist → schedule → `202 Acknowledgement` |
| GET | `/v1/hospital-api/claims/{claim_ref}` | returns `StatusUpdate` mapped from sim state (01-02 §4.3) |
| POST | `/v1/hospital-api/claims/{claim_ref}/documents` | validates `DocumentRef[]`, stores, `202`; may trigger scenario `on: documents_received` |
| GET | `/v1/hospital-api/claims/{claim_ref}/queries` | lists `sim_query` rows |
| POST | `/v1/hospital-api/queries/{query_id}/responses` | validates `QueryResponse`; marks answered; triggers `on: query_response` steps |
| POST | `/v1/hospital-api/claims/{claim_ref}/withdraw` | state → `closed`, cancel pending events, optional final status callback |
| GET | `/v1/health`, `/v1/contract` | standard |

Request/response examples:

Submission (abbreviated; models per 01-02):
```http
POST /v1/hospital-api/claims
X-Contract-Version: 1.0
X-Key-Id: hosp-001
X-Timestamp: 2026-10-06T10:15:30Z
X-Idempotency-Key: 6f1d1e5e-9a1e-4a54-9f3d-0f3a3d9b1c11
X-Signature: <base64 hmac>
Content-Type: application/json

{"contract_version":"1.0","claim_ref":"HC-2026-000123","claim_type":"cashless","patient":{...},"admission":{...},
 "bill_lines":[...],"totals":{"gross":"184500.00","discounts":"4500.00","claimed":"180000.00"},"documents":[...],
 "config_versions":{"doc_requirements":4,"deadlines":2,"router_rules":3,"confidence_gates":1},"submitted_at":"2026-10-06T10:15:29Z"}
```
Response:
```json
HTTP/1.1 202 Accepted
{"insurer_claim_no":"IC-2026-000017","claim_ref":"HC-2026-000123","received_at":"2026-10-06T10:15:31Z","status":"received","scenario":"query_twice_then_approve"}
```
(`scenario` is an extra field gated by `TPA_SIM_EXPOSE_SCENARIO=1`; the real insurer never returns it. Hospital code must ignore unknown fields — contract policy.)

Callback example (sim → hospital):
```http
POST {HOSP_BASE_URL}/v1/insurer-callbacks/queries
X-Key-Id: ins-001  X-Timestamp: ...  X-Idempotency-Key: <uuid derived from claim_ref+sequence>  X-Signature: ...
{"claim_ref":"HC-2026-000123","sequence":3,"query":{"query_id":"q-IC-2026-000017-1","round":1,"category":"missing_document",
  "text":"Please provide the implant sticker for the knee prosthesis.","requested_doc_types":["implant_sticker"],
  "due_by":"2026-10-13T10:15:31Z","status":"open"}}
```
The idempotency key for callbacks is deterministic (`uuid5(claim_ref, sequence, kind)`) so retries and duplicate injection use the same key — mirrors how the real system should behave.

### 4.2 Control plane (`/sim/*`; bound to 127.0.0.1/compose network only; no HMAC; optional bearer `SIM_ADMIN_TOKEN`)

| Method | Path | Description |
|---|---|---|
| GET | `/sim/scenarios` | list ids, names, steps summary |
| GET | `/sim/scenarios/{id}` | full YAML |
| POST | `/sim/scenarios` | upload/replace scenario YAML (validates DSL) |
| DELETE | `/sim/scenarios/{id}` | remove custom scenario (built-ins protected) |
| GET | `/sim/claims` | list claims with state, next event, sequence |
| GET | `/sim/claims/{ref}` | full detail incl. events, queries, log |
| POST | `/sim/claims/{ref}/fire` | fire next scheduled event now (body `{count:1}`) |
| POST | `/sim/claims/{ref}/override` | `{scenario_id?, time_scale?, decision?, approved_ratio?, skip_steps?[]}` |
| POST | `/sim/claims/{ref}/pause` / `/resume` | stop/start scheduler for the claim |
| POST | `/sim/claims/{ref}/inject` | send an arbitrary callback now (`kind`, `payload`, `bad_signature?`, `timestamp_offset_s?`) |
| POST | `/sim/chaos` | global: `{drop_rate, duplicate_rate, delay_ms, jitter_ms, out_of_order, reorder_window, fail_status}`; `DELETE` clears |
| GET | `/sim/log` | recent messages (filters: claim_ref, direction, limit) |
| POST | `/sim/time-scale` | `{value: 0.1}` |
| POST | `/sim/reset` | wipe claims/events/log (keep scenarios) |
| GET | `/sim/export/{ref}` | JSON bundle (claim, events, log) for bug reports |

Errors from the control plane use the same RFC 7807 shape.

### 4.3 UI (`/` and `/ui/claims/{ref}`)

Single server-rendered page (Jinja + htmx; no build step):
- Table: claim_ref, insurer_claim_no, scenario, state, round, sequence, next event (countdown), actions.
- Row actions: Fire next, Pause/Resume, Override scenario (dropdown), Inject callback (modal), Withdraw.
- Detail page: timeline (inbound/outbound messages with HTTP status and signature result), query list with responses, raw payload viewer.
- Global bar: time scale slider, chaos panel with presets (`off`, `flaky`, `slow`, `hostile`), reset button.

---

## 5. Build tasks

1. **Scaffold** `services/tpa-sim/`: `app/main.py`, `app/api/{insurer.py,control.py,ui.py}`, `app/core/{config.py,security.py,idempotency.py,validation.py}`, `app/engine/{dsl.py,scheduler.py,actions.py,templates.py,chaos.py}`, `app/db/{models.py,session.py}`, `scenarios/*.yaml`, `scenarios/query_templates.yaml`, `templates/*.html`, `tests/`. Add dependency on `claim_contract` workspace package. Verify: `uvicorn app.main:app` starts; `/v1/health` returns ok.
2. **Insurer endpoints with security**: reuse `claim_contract.signing.verify`, timestamp window check, key-id lookup (`HOSP_KEYS={hosp-001: secret}`), idempotency (`claim_contract` middleware + `sim_idempotency` table). Return contract errors for bad signature/stale timestamp. Verify: tampered-body test returns 401 `invalid_signature`.
3. **Persistence + scheduler**: SQLAlchemy models (section 3), Alembic-less `create_all` is acceptable for a simulator (PROPOSED). Scheduler `asyncio` task polls `sim_event` every 500 ms (`due_at <= now`, not paused), claims events with `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres) or an in-process lock (SQLite). Fake-clock abstraction `Clock` for tests. Verify: unit test with `FakeClock`.
4. **Scenario DSL engine** `app/engine/dsl.py`: parse YAML into steps (section 6.2), validate (JSON Schema), compile `at:` offsets (relative to previous step or claim receipt) and `on:` triggers (`query_response`, `documents_received`, `withdraw`, `timeout`). Verify: invalid DSL rejected with line numbers.
5. **Actions** `app/engine/actions.py`: `ack`, `status`, `query`, `decision`, `settlement`, `escalate`, `wait`, `probe_bad_signature`, `noop`. Each builds contract models from `claim_contract`, never raw dicts. Verify: outputs validate against OpenAPI schemas.
6. **Callback sender**: wrap `claim_contract.OutboxSender` (retries 1s,4s,16s,60s,5m; max 8; `TPA_SIM_RETRY_SCALE` shrinks backoff for tests) with sequence assignment at **send time** unless chaos reorders. Record every attempt in `sim_log`. Verify: hospital 500 then 200 results in one effect.
7. **Built-in scenarios** (section 6.3) as YAML files with tests asserting expected event sequences under a fake clock. Prioritise `happy_path` and `query_twice_then_approve` first (unblocks Dev A).
8. **Chaos module** `app/engine/chaos.py`: probabilistic drop (callback not sent but marked `dropped`; retried per contract because hospital never acknowledges? see 6.6), duplicate (send twice), delay/jitter, out-of-order (hold events in a window and release shuffled), `fail_status` (pretend sim endpoint returns 503 for N calls). Seeded RNG (`CHAOS_SEED`) for reproducibility.
9. **Submission validation**: call the shared validator in `claim_contract.validation` (totals reconcile, dates, ICD pattern, required docs per config version if provided) and return 422 with the same codes. Optional doc download check: `HEAD`/`GET` presigned URL, verify `sha256` when `TPA_SIM_VERIFY_DOCS=1`; failure → 424 `doc_unavailable`.
10. **Control plane + UI** (section 4.2/4.3).
11. **Query templates** `scenarios/query_templates.yaml` (6.4) and optional LLM mode via gateway alias `reason-local` (off by default).
12. **Bank/TPA profiles** (section 6.5) to vary behaviour per simulated insurer (response time, query habits).
13. **`hospital-sim` fixture** `insurer/tests/fixtures/hospital_sim.py` (section 9.6) plus pytest plugin.
14. **Dockerfile + compose** entry in `infra/compose/shared.yml` (profile `sim`), healthcheck, `mem_limit 256m`, volume for SQLite.
15. **Docs**: `services/tpa-sim/README.md` with a 10-minute quickstart for Dev A and a scenario cookbook.
16. **Tests** (section 9).

---

## 6. Key logic

### 6.1 Processing a submission

```
verify X-Contract-Version (major == 1)
verify key id, timestamp window (±300s), signature (constant-time)          -> 401 on failure
idempotency lookup (key_id, idem_key):
    same hash   -> replay stored response (Idempotent-Replay: true)
    diff hash   -> 409 idempotency_conflict
claim_ref already exists with a different idempotency key:
    same payload_hash -> return existing ack (200/202, treated as replay)
    different hash    -> 409 idempotency_conflict   (hospital must use the supplement endpoint)
validate ClaimSubmission (schema, totals, dates, config versions known)     -> 422 on failure
optional: verify documents downloadable and sha256 match                     -> 424 doc_unavailable
choose scenario: header X-Sim-Scenario > override table by claim_ref > match rules (first match) > TPA_SIM_DEFAULT_SCENARIO
allocate insurer_claim_no
insert sim_claim, compile and insert sim_event rows for all time-based steps (event-based steps wait)
store idempotent response; return 202 Acknowledgement
```
The `ack` itself is returned synchronously; the scenario's `ack` step also fires a `status: received` callback so hospital code exercises both paths.

### 6.2 Scenario DSL (YAML, PROPOSED)

Top-level:
```yaml
id: query_twice_then_approve
name: Two query rounds, then partial approval and settlement
description: Exercises the query loop twice, partial decision with deductions, and settlement callback.
match:                       # optional; evaluated in file-sorted order if no header/override
  claim_type: cashless
  total_claimed_gte: 100000
  claim_ref_regex: "^HC-2026-0001"
profile: default             # insurer behaviour profile (6.5)
steps:
  - id: s1
    at: +5s                  # relative to claim receipt (default anchor: receipt)
    do: status
    value: received
  - id: s2
    at: +20s
    do: status
    value: verifying
  - id: s3
    at: +40s
    do: query
    round: 1
    category: missing_document
    requested_doc_types: [implant_sticker]
    text: "Please provide the implant sticker."      # or template: missing_document.implant
    due_in: 7d
  - id: s4
    on: query_response       # trigger: response to the latest open query
    delay: +15s
    do: query
    round: 2
    category: medical_clarification
    text: "Justify ICU stay of 4 days."
  - id: s5
    on: query_response
    delay: +10s
    do: status
    value: awaiting_approval
  - id: s6
    after: s5 +20s           # relative to another step
    do: decision
    outcome: partial
    approved_ratio: 0.85     # approved_amount = round(claimed * ratio, 2)
    deductions:
      - {rule: room_rent_cap, amount: "4200.00", explanation: "Room rent exceeded 1% of SI"}
    reason_codes: [RR_CAP]
  - id: s7
    after: s6 +30s
    do: settlement
    utr: random              # or fixed value; random = 'UTR' + 12 digits seeded
    amount_from: decision    # settle the approved amount
    bank: HDFC-sim
```

Supported triggers: `at: +Ns` (anchor receipt), `after: <step_id> +Ns`, `on: query_response | documents_received | withdraw | timeout(<query round>)`, `on_not: query_response within +Ns` (timeout branch).

Supported actions and parameters:

| `do` | Parameters | Callback emitted |
|---|---|---|
| `status` | `value` ∈ InsurerCaseStatus | `/status` (mapped to hospital-visible status, 01-02 §4.3) |
| `query` | `round`, `category`, `text|template`, `requested_doc_types`, `due_in` | `/queries` + `/status under_query` |
| `decision` | `outcome` (approve/partial/reject), `approved_ratio` or `approved_amount`, `deductions[]`, `reason_codes[]`, `reviewers` | `/decisions` |
| `settlement` | `utr`, `amount`, `amount_from`, `bank`, `paid_at` | `/settlements` |
| `escalate` | `reason` | `/status escalated` (hospital shows `under_query`) |
| `probe_bad_signature` | `kind` | callback signed with wrong secret (hospital must reject) |
| `wait` | `for` | none |
| `close` | none | `/status closed` |

Branching: `if: {claim_type: reimbursement}` per step; `else` allowed via two steps with opposite conditions. Loops are not supported (keep the DSL small).

Semantics:
- Steps execute in file order; `on:` steps remain pending until the trigger occurs; `at:` and `after:` steps are materialised as `sim_event` rows at claim receipt or when their anchor fires.
- `sequence` is allocated at fire time, increasing by 1 per callback per claim.
- State changes are validated against 01-02 §4.2 transitions; an invalid scenario transition raises `scenario_invalid_transition` on upload (static check) so scenario authors cannot create impossible flows.
- `TPA_SIM_TIME_SCALE` multiplies all durations (0.1 = 10× faster). Applied when scheduling; changes affect only future events.

### 6.3 Built-in scenarios

| id | Behaviour | Purpose / who uses it |
|---|---|---|
| `happy_path` | ack → verifying (+10s) → awaiting_approval → approve full amount (+60s) → settlement (+30s) | baseline flow, UI demo |
| `query_twice_then_approve` | round-1 missing_document, round-2 medical_clarification, approve partial with deductions, settle | query loop, 01-02 hospital transitions `under_query ⇄ acknowledged` |
| `query_three_rounds_escalate` | rounds 1-3; round 3 unanswered for `due_in` → `escalated` status; then human-style decision after manual `/fire` | escalation display, round-3 handling |
| `partial_with_deductions` | partial approval with 3 deduction lines (room rent cap, non-payable consumables, co-pay) | decision display, calculation breakdown UI |
| `reject_exclusion` | rejection with reason codes (`EXCL_PREEXISTING`) | rejection path, appeal flow later |
| `identity_mismatch` | query round 1 category `identity_mismatch` requesting `id_proof`; after response proceed to approval | identity handling |
| `slow_insurer` | long delays (acks after 5 min; queries 20 min), no `due_by` extension | reminders/SLA tests (use time scale to compress) |
| `flaky_callbacks` | happy path but chaos preset: 20% drop, 20% duplicate, out-of-order window 3 | idempotency/ordering tests |
| `bad_signature_probe` | first callback signed with wrong secret, second with timestamp skew +10 min, third valid | hospital must reject first two and accept third |
| `needs_docs_then_reject` | missing_document queries unanswered → reject for non-submission | negative path |
| `withdrawn_by_hospital` | waits for `/withdraw`, then closed | withdrawal handling |
| `reimbursement_basic` | reimbursement flow: ack, query for cancelled cheque (`cancelled_cheque`), approve, settlement with UTR | claim_type=reimbursement |
| `duplicate_decision` | sends the decision twice with the same sequence | duplicate handling |
| `late_settlement` | settlement 48 simulated hours later (time-scaled) | long waits, reminders |

Each built-in YAML is accompanied by `tests/test_scenario_<id>.py` asserting its callback list and state progression using `FakeClock`.

### 6.4 Query generation

Templates in `scenarios/query_templates.yaml`:
```yaml
missing_document:
  default: "Please provide the following document(s): {doc_list}."
  implant: "Please provide the implant sticker/invoice for {procedure}."
  preauth: "Pre-authorisation reference {preauth_ref} could not be matched. Please share the approval letter."
medical_clarification:
  default: "Please clarify the medical necessity of {item} for diagnosis {diagnosis}."
  icu: "Justify ICU stay of {days} days with supporting clinical notes."
billing_discrepancy:
  default: "Bill total {gross} does not match itemised lines {lines_total}. Please reconcile."
identity_mismatch:
  default: "Patient name/date of birth on {doc} differs from the policy record. Please provide a valid ID proof."
illegible_document:
  default: "The {doc} is not legible. Please resubmit a clear copy."
policy_exclusion:
  default: "The condition appears to fall under exclusion {clause}. Please provide treating doctor's justification."
other:
  default: "Additional information required: {text}."
```
Variables filled from the submitted claim (procedure names, diagnosis, totals). If `TPA_SIM_LLM=1`, the text is generated by `reason-local` from claim facts with a fixed seed prompt; the output is stored in `sim_query.text` so replays are deterministic after first generation.

### 6.5 Insurer/bank profiles (PROPOSED)

A profile adjusts timing and style without changing scenario logic. Files in `scenarios/profiles/*.yaml`:

```yaml
id: fast_tpa
latency_multiplier: 0.5
query_habits: {max_rounds: 2, prefers: [missing_document, billing_discrepancy]}
settlement: {bank: "SBI-sim", delay: "+20s", utr_format: "SBIN{12digits}"}
due_in_default: 3d
```
Shipped profiles: `default`, `fast_tpa`, `slow_tpa`, `strict_reviewer` (adds extra clarification round), `hdfc_bank_sim`, `icici_bank_sim` (different UTR formats and settlement delays). Used by settlement callbacks and by the hospital's reconciliation tests (hospital must parse different UTR formats).

### 6.6 Chaos semantics

| Setting | Effect | Hospital expectation |
|---|---|---|
| `drop_rate` p | with probability p an outbound callback attempt is not sent (logged `dropped`) and retried by the sender per contract backoff | final state correct, one effect |
| `duplicate_rate` p | callback sent twice with same idempotency key and sequence | applied once |
| `delay_ms`, `jitter_ms` | delay before send | no timeouts in hospital UI beyond SLA |
| `out_of_order` + `reorder_window=N` | hold N events and release shuffled (sequence numbers preserved) | hospital ignores lower-or-equal sequences already applied; handles gaps (stores pending) |
| `fail_status` | sim's own insurer endpoints return 503 for next N calls | hospital outbox retries with same idempotency key |
| `bad_signature` (via inject) | wrong secret | hospital 401, not applied |
| `timestamp_offset_s` (via inject) | skewed timestamp | hospital rejects `stale_request` |

Chaos RNG seeded by `CHAOS_SEED` (default 42) plus claim_ref hash, so failures are reproducible. Chaos is off by default and cleared by `/sim/reset`.

Note on drops: "dropped" means the first attempt is lost; because the sender has not seen a 2xx it retries — that is the contract's at-least-once behaviour. Permanent drop (never delivered) is simulated by `drop_rate: 1.0` with `max_attempts: 1` to test dead-letter handling in hospital-api (the hospital must surface "no update from insurer" via polling `GET claims/{ref}`).

### 6.7 Responding to hospital actions

- Query response received: validate (`query_id` exists and `open`, referenced `attached_doc_ids` exist in the submitted documents or supplements), set `answered`, store response, emit trigger `query_response`.
- Supplementary documents: store; if a scenario waits `on: documents_received`, trigger it.
- Withdraw: set `withdrawn=true`, cancel pending events, emit `/status closed` (scenario `withdrawn_by_hospital` shows the flow); further hospital calls on that claim return 409 `invalid_transition`.
- Status poll: always consistent with the last callback sent (even if chaos dropped it), so hospitals relying on polling can self-heal.

### 6.8 Restart and recovery

On startup: load `sim_event` rows with `fired_at IS NULL`; fire overdue ones in `due_at` then `id` order (respecting per-claim sequence ordering); mark `attempts` correctly. Paused claims stay paused. `TPA_SIM_TIME_SCALE` is persisted in `sim_config` so a restart doesn't change pacing.

---

## 7. Configuration

```
TPA_SIM_PORT=8500
TPA_SIM_DB_URL=sqlite+aiosqlite:///data/tpa_sim.db
HOSP_BASE_URL=http://hospital-api:8000
HOSP_TO_INS_HMAC_SECRET=...            # verify inbound
INS_TO_HOSP_HMAC_SECRET=...            # sign outbound
HOSP_KEYS=hosp-001:...                 # key id -> secret (multiple allowed, supports rotation)
INS_KEY_ID=ins-001
TPA_SIM_DEFAULT_SCENARIO=happy_path
TPA_SIM_DEFAULT_PROFILE=default
TPA_SIM_TIME_SCALE=1.0                 # 0.1 = 10x faster demos/tests
TPA_SIM_RETRY_SCALE=1.0                # shrink retry backoff in tests
TPA_SIM_EXPOSE_SCENARIO=1              # extra field in ack for debugging
TPA_SIM_VERIFY_DOCS=0
TPA_SIM_LLM=0                          # 1 = generate query text via llm-gateway alias reason-local
LLM_GATEWAY_URL=http://llm-gateway:4000   LLM_GATEWAY_KEY=...
SIM_ADMIN_TOKEN=                        # optional bearer for /sim/*
CHAOS_SEED=42
LOG_LEVEL=info
```

---

## 8. Edge cases and failure handling

| # | Situation | Behaviour |
|---|---|---|
| 1 | Duplicate submission, same idempotency key and body | Replay stored 202 with `Idempotent-Replay: true`. |
| 2 | Same claim_ref, new key, different payload | 409 `idempotency_conflict` (hospital should use supplement endpoint); same payload → treated as replay. |
| 3 | Same idempotency key, different body | 409 `idempotency_conflict`. |
| 4 | Invalid signature or tampered body | 401 `invalid_signature`; logged with `signature_ok=false`. |
| 5 | Timestamp older/newer than 300 s | 401 `stale_request`. |
| 6 | Unsupported contract major | 400 `unsupported_version`. |
| 7 | Totals mismatch | 422 `totals_mismatch` (same as real insurer). |
| 8 | Query response for unknown/closed query | 404 `unknown_query` / 409 `invalid_transition`. |
| 9 | Query response references doc id not in claim | 422 `validation_error` field `attached_doc_ids`. |
| 10 | Hospital callback endpoint down | Retry per contract; after 8 attempts event `failed`, visible in UI with "Retry" button; claim continues (state advanced) so hospital polling can recover. |
| 11 | Hospital returns 4xx to a callback | No retries (client error); logged with response body for diagnosis (usually signature/validation bug in hospital). |
| 12 | Presigned document URL expired/unreachable | With `TPA_SIM_VERIFY_DOCS=1` the submission returns 424 `doc_unavailable` to exercise hospital's refresh flow. |
| 13 | Clock skew test | `inject` with `timestamp_offset_s` produces stale callbacks. |
| 14 | Time scale changed mid-run | Only future events are scheduled with new scale; existing `due_at` unchanged. Documented in UI tooltip. |
| 15 | Restart mid-scenario | Reload pending events; fire overdue in order; sequences remain monotonic (stored). |
| 16 | Scenario upload with impossible transition | Rejected 422 with the offending step. |
| 17 | Two scenarios match | First by file-sorted order; log which matched; header/override always wins. |
| 18 | Hospital submits claim types the scenario does not handle (reimbursement into cashless-only scenario) | Fallback to `reimbursement_basic` or default; warning in log and `sim_claim.override_json.warnings`. |
| 19 | Concurrent control-plane `fire` and scheduler | Event claimed by lock; double fire prevented; second call returns 409 `event_already_fired`. |
| 20 | Sequence gap due to dropped permanent callback | Status poll endpoint returns latest; sequence of the next callback continues, so hospital must tolerate gaps (contract §8 says ignore lower-or-equal only). |
| 21 | Memory/disk growth in long demos | `sim_log` capped (default 20k rows, ring buffer). |
| 22 | Settlement amount inconsistent with decision | `amount_from: decision` prevents; explicit mismatch allowed in a negative scenario `settlement_mismatch` to test hospital reconciliation alerts. |
| 23 | Non-ASCII/Unicode names in submissions | Stored as-is; HMAC computed on raw bytes; test with Devanagari name. |
| 24 | Oversized body (> 2 MB) | 413 per contract §9. |
| 25 | Rate limit | 100 req/min per key id → 429 `rate_limited` (configurable `TPA_SIM_RATE_LIMIT`; set 0 to disable in load tests). |

---

## 9. Tests

### 9.1 Test matrix

| ID | Area | Test | Expected |
|---|---|---|---|
| T1 | Contract | schemathesis against tpa-sim insurer endpoints | no schema violations |
| T2 | Security | valid signature accepted; tampered body 401; wrong key 401 | pass |
| T3 | Security | stale timestamp, future timestamp | 401 `stale_request` |
| T4 | Idempotency | replay/conflict cases (items 1-3 of section 8) | pass |
| T5 | Validation | totals mismatch, date errors, unknown config version | 422 with right codes |
| T6 | Engine | DSL parse: valid/invalid files, line-numbered errors | pass |
| T7 | Engine | `at`/`after`/`on` scheduling under `FakeClock` | exact due times |
| T8 | Engine | time scale 0.1 compresses durations | pass |
| T9 | Engine | impossible transition rejected on upload | 422 |
| T10 | Callbacks | per scenario, ordered callback list and sequences match golden file | pass for all 14 built-ins |
| T11 | Callbacks | each callback validates against contract models | pass |
| T12 | Callbacks | signed with insurer secret; hospital stub verifies | pass |
| T13 | Query loop | response triggers next round; round 3 unanswered escalates (`timeout` trigger) | pass |
| T14 | Chaos | 20% drop + 20% duplicate: stub receiver ends with each event applied once (dedupe by idem key + sequence) | pass |
| T15 | Chaos | out-of-order window: receiver ignoring lower sequences ends in correct state | pass |
| T16 | Chaos | seeded RNG reproducible | same log twice |
| T17 | Recovery | restart mid-scenario resumes without duplicate or gap | pass |
| T18 | Control plane | `fire`, `pause`, `override`, `inject`, `reset` behave | pass |
| T19 | UI | claims table renders; fire button works (Playwright smoke) | pass |
| T20 | Docs check | `TPA_SIM_VERIFY_DOCS=1` with expired URL → 424 | pass |
| T21 | Withdraw | withdraw cancels pending events; later calls 409 | pass |
| T22 | Profiles | UTR formats per profile; delay multipliers | pass |
| T23 | LLM mode | `TPA_SIM_LLM=1` stores generated text and replays deterministically | pass |
| T24 | Performance | 200 claims concurrently scheduled; scheduler lag < 2 s | pass |

### 9.2 Cross-team end-to-end (run in Dev A's integration suite)
For every built-in scenario: submit a synthetic claim from hospital-api, run with `TPA_SIM_TIME_SCALE=0.05`, assert the hospital case status path equals the allowed transitions in 01-02 §4.1 and final audit log contains expected events (`ack.received`, `query.received`, `decision.*`, `settlement.recorded`). A full happy-path flow must complete in < 1 minute at time scale 0.1.

### 9.3 Hospital-negative checks (probe scenarios)
`bad_signature_probe`: hospital must record rejects (`invalid_signature`, `stale_request`) in audit and apply only the valid third callback.
`duplicate_decision`: exactly one `decision.applied` event.
`settlement_mismatch`: hospital raises a reconciliation flag.

### 9.4 Fake clock
`app/core/clock.py` defines `Clock` (`now()`, `sleep_until()`); production uses wall time × `TPA_SIM_TIME_SCALE`; tests inject `FakeClock.advance(seconds)` to run whole scenarios in milliseconds.

### 9.5 Golden files
`tests/golden/<scenario>.jsonl` store expected `(offset_s, kind, key fields)`; regenerate with `make tpasim-golden` and review diffs in PRs.

### 9.6 `hospital-sim` fixture (for Dev B)

Path: `insurer/tests/fixtures/hospital_sim.py`; a pytest plugin offering:
```python
@pytest.fixture
async def hospital_sim(insurer_api_url, hmac_secrets):
    sim = HospitalSim(
        base_url=insurer_api_url,
        key_id="hosp-001",
        secret=hmac_secrets.h2i,
        callback_secret=hmac_secrets.i2h,
    )  # starts a local receiver on a free port
    await sim.start()
    yield sim
    await sim.stop()
```
API:
- `sim.make_claim(scenario="knee_replacement_ok", claim_type="cashless", **overrides) -> ClaimSubmission` built from synthetic cases (`data/synthetic`).
- `await sim.submit(claim, idempotency_key=None)` signs and posts; returns ack.
- `sim.received` list of validated callbacks with `sequence` and idempotency-key checks (asserts monotonic sequences, flags duplicates).
- `await sim.respond_to_query(query_id, text, attach=[...])` posts `QueryResponse`.
- Receiver verifies signatures, enforces idempotency, supports chaos toggles (`reject_next(n, status)`) to test insurer retry.
- Assertions helpers: `sim.assert_status_path(["received","verifying","needs_info",...])`.

Tests for the fixture itself are in `insurer/tests/test_hospital_sim.py`.

---

## 10. Acceptance criteria

- [ ] Dev A can run all built-in scenarios end to end against hospital-api.
- [ ] With time scale 0.1 the happy path completes in under a minute.
- [ ] Contract suite (schemathesis) green; signing vectors match the shared library.
- [ ] Chaos presets `flaky` and `hostile` leave the hospital in the correct final state with each event applied once.
- [ ] Restart recovery works (T17).
- [ ] No verification/calculation logic duplicated from insurer-api (code review check: scenario files are the only source of outcomes).
- [ ] UI shows state, next event, message log, and supports fire/override/inject.
- [ ] README quickstart lets Dev A run the first scenario in under 10 minutes.
- [ ] `hospital-sim` fixture usable by Dev B and covered by tests.
- [ ] Deliverables 1-7 (milestone "tpa-sim v0": `happy_path`, `query_twice_then_approve`) available at end of Phase 0/early Phase 1 to unblock Dev A.

---

## 11. Dependencies

| Depends on | For |
|---|---|
| `01-shared-contract/*` (frozen v1 at end of Phase 0) | models, signing, idempotency, error shapes, OutboxSender |
| Redis (04-01) | optional idempotency/cache backend (SQLite table is the default; Redis used when `TPA_SIM_IDEMPOTENCY=redis`) |
| llm-gateway (04-04) | optional LLM query text |
| `data/synthetic` (05-integration/01) | cases for `hospital-sim` and demo claims |
| hospital-api (Dev A) | callback receiver for end-to-end tests |

Consumed by: Dev A (all phases), integration milestone M1-M4 (05/05), evaluation harness (scripted insurer responses), demos.

Interface freeze notes: scenario DSL v1 and `/sim/*` control endpoints are internal to the two developers; the insurer-facing endpoints must remain contract-identical. Any change in the contract bumps `X-Contract-Version` handling here the same day.

---

## 12. Claude Code kickoff prompt

> Implement docs/implementation/04-shared-services/06-tpa-sim.md. Deliver in two drops: **Drop 1 (tasks 1-7, only `happy_path` and `query_twice_then_approve`)** so Dev A is unblocked, with tests T1-T13 for those; then **Drop 2** (remaining scenarios, chaos, profiles, UI, `hospital-sim` fixture, tests T14-T24). Use the `claim_contract` package for models, signing, idempotency and OutboxSender; do not redefine them. Keep tpa-sim scripted — no verification or calculation logic. Run every scenario under FakeClock and commit the golden files. Mark any new decisions PROPOSED in the doc.
