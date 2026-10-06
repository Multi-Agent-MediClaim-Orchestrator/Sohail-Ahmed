# 02-06 — hospital-api: Claim Builder Trigger, Officer Sign-off and Submission Client

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/{routers/claims.py,services/claim_builder.py,services/submission.py,outbox/}`.

## 1. Goal
Turn a `docs_complete` case into a validated `ClaimSubmission` (contract 01-02): trigger the Claim Builder agent (doc 09), validate its output with deterministic code, let an Officer review/edit and sign off, then deliver to the insurer reliably (HMAC, idempotency, outbox, retries) and track acknowledgement.

Design stance:
- **The agent proposes, code disposes.** The Claim Builder crew returns a draft; only deterministic validators decide whether it may be signed off. An LLM never gates anything.
- **Crews never write DB.** Output arrives at an internal endpoint, is validated against a strict Pydantic model, and persisted by the API.
- **Exactly-once effect, at-least-once delivery.** Transactional outbox + idempotency key means crashes and retries cannot duplicate a submission at the insurer.
- **Human authority.** No submission without a sign-off bound to the exact draft version being sent; any later edit invalidates it.

### 1.1 Lifecycle
```
docs_complete ──build──▶ building_claim ──draft_ready──▶ ready_for_review
                                    ▲                          │ officer edits (new version, signoff invalidated)
                                    │ repair loop (≤2)         │ officer signoff(approved, ack warnings)
                                    └──── validation errors ───┤
                                                               ▼
                                         submit ──(tx: outbox row + status)──▶ submitted
                                                               │ outbox worker (HMAC POST)
                                                               ▼
                                                       acknowledged ──callbacks──▶ under_query ⇄ acknowledged
                                                               ▼
                                                  approved|partially_approved|rejected ──▶ settled ──▶ closed
```

## 2. Inputs / Outputs
- In: case, parsed typed JSON of documents, route decision, config versions.
- Out: `claim_draft` versions, `bill_line` rows, `signoff`, `outbox` message, `insurer_claim_no`, status `submitted → acknowledged`.

## 3. Data model
Tables: `claim_draft`, `bill_line`, `signoff`, `outbox`, `inbound_callback` (doc 01). Draft `validation` JSON: `{"errors":[{code,field,message}],"warnings":[...],"reconciliation":{"lines_sum":"…","bill_total":"…","diff":"0.00"}}`.

### 3.1 DDL recap with columns this doc relies on
```sql
CREATE TABLE claim_draft (
  id            uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id       uuid NOT NULL REFERENCES claim_case(id),
  version       int  NOT NULL,
  source        text NOT NULL CHECK (source IN ('agent','human_edit','repair')),
  payload       jsonb NOT NULL,             -- ClaimDraftIn (patient, admission, bill_lines, totals)
  provenance    jsonb NOT NULL,             -- field path -> {doc_id,page,confidence}
  validation    jsonb NOT NULL,             -- errors/warnings/reconciliation
  has_errors    boolean NOT NULL,
  model_info    jsonb,                      -- alias, prompt_version, trace_id (agent drafts only)
  edit_summary  jsonb,                      -- {"fields_changed":["bill_lines[3].amount"]} names only
  created_by    text NOT NULL,              -- 'agent:claim-builder' | user id
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, version)
);

CREATE TABLE bill_line (
  id          uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  draft_id    uuid NOT NULL REFERENCES claim_draft(id) ON DELETE CASCADE,
  line_no     int NOT NULL,
  code        text,
  description text NOT NULL,
  category    text NOT NULL,
  qty         numeric(12,3) NOT NULL,
  unit_price  numeric(14,2) NOT NULL,
  amount      numeric(14,2) NOT NULL,
  source_doc_id uuid NOT NULL REFERENCES document(id),
  source_page int,
  UNIQUE (draft_id, line_no)
);

CREATE TABLE signoff (
  id            uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id       uuid NOT NULL REFERENCES claim_case(id),
  draft_id      uuid NOT NULL REFERENCES claim_draft(id),
  decision      text NOT NULL CHECK (decision IN ('approved','returned')),
  comment       text,
  acknowledged_warnings text[] NOT NULL DEFAULT '{}',
  signed_by     uuid NOT NULL REFERENCES app_user(id),
  signed_at     timestamptz NOT NULL DEFAULT now(),
  invalidated_at timestamptz,
  invalidated_reason text
);
CREATE UNIQUE INDEX ux_signoff_active ON signoff (draft_id) WHERE decision='approved' AND invalidated_at IS NULL;

CREATE TABLE outbox (
  id               uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id          uuid NOT NULL REFERENCES claim_case(id),
  kind             text NOT NULL,           -- claim.submit | claim.documents | query.response | claim.withdraw
  method           text NOT NULL,
  path             text NOT NULL,
  body             jsonb NOT NULL,
  body_sha256      char(64) NOT NULL,
  idempotency_key  uuid NOT NULL UNIQUE,
  status           text NOT NULL CHECK (status IN ('pending','sending','sent','failed','dead')),
  attempts         int NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  last_error       jsonb,
  response_status  int,
  response_body    jsonb,
  sequence         bigint NOT NULL,         -- per-claim monotonic, sent in header for ordering
  created_at       timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz
);
CREATE INDEX ix_outbox_due ON outbox (next_attempt_at) WHERE status IN ('pending','sending');

CREATE TABLE inbound_callback (
  id           uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  claim_ref    text NOT NULL,
  kind         text NOT NULL,               -- status|query|decision|settlement
  sequence     bigint NOT NULL,
  idempotency_key uuid NOT NULL,
  body         jsonb NOT NULL,
  response_status int NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (claim_ref, kind, sequence)
);

-- migration 0013
ALTER TABLE claim_case ADD COLUMN decision jsonb, ADD COLUMN insurer_claim_no text, ADD COLUMN late_filing boolean NOT NULL DEFAULT false;
CREATE TABLE settlement (
  id uuid PRIMARY KEY DEFAULT gen_uuid_v7(), case_id uuid NOT NULL REFERENCES claim_case(id),
  utr text NOT NULL, amount numeric(14,2) NOT NULL, paid_on date NOT NULL, raw jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (case_id, utr));
```

## 4. API / endpoints
```
POST /v1/cases/{id}/claim/build          desk|officer -> 202 {job_id}; requires docs_complete
GET  /v1/cases/{id}/claim                latest draft + validation + provenance per field
GET  /v1/cases/{id}/claim/versions
PUT  /v1/cases/{id}/claim                officer edit (If-Match draft version) -> new version source=human_edit
POST /v1/cases/{id}/claim/validate       re-run validators on current draft
POST /v1/cases/{id}/claim/signoff        officer {decision:"approved"|"returned", comment}
POST /v1/cases/{id}/claim/submit         officer; requires signoff on latest version -> 202
GET  /v1/cases/{id}/submission           outbox status, attempts, ack
POST /v1/cases/{id}/submission/retry     officer; for dead-lettered
POST /v1/cases/{id}/claim/withdraw       officer {reason}
Internal (svc-crew): POST /v1/internal/cases/{id}/claim/draft   {payload, provenance, model_info}
Insurer callbacks (HMAC): see doc 07 for queries; here: /v1/insurer-callbacks/status, /decisions, /settlements
```
Draft payload excerpt:
```json
{"patient":{...},"admission":{...},"bill_lines":[{"line_no":1,"code":"ROOM-PVT","description":"Private room x4 days","category":"room","qty":"4","unit_price":"5000.00","amount":"20000.00","source_doc_id":"...","source_page":2}],
 "totals":{"gross":"184500.00","discounts":"4500.00","claimed":"180000.00"},
 "provenance":{"patient.dob":{"doc_id":"...","page":1,"confidence":0.97}}}
```

### 4.1 Examples
**Build**
```http
POST /v1/cases/6f1c…/claim/build
→ 202 {"job_id":"cb-20261006-0041","status":"queued","case_status":"building_claim"}
→ 409 {"code":"invalid_transition","detail":"case is docs_pending; completeness has 2 blockers"}
```
**Get draft** (abridged)
```json
{"case_id":"6f1c…","version":3,"source":"human_edit","has_errors":false,
 "payload":{"totals":{"gross":"184500.00","discounts":"4500.00","claimed":"180000.00"},"bill_lines":[ … 27 lines … ]},
 "validation":{"errors":[],
   "warnings":[{"code":"V10","field":"totals.claimed","message":"Claimed 1,80,000 exceeds pre-auth 1,50,000 by 20%","ack_required":true},
               {"code":"V09","field":"bill_lines[14]","message":"Possible duplicate of line 13","ack_required":true}],
   "reconciliation":{"lines_sum":"184500.00","bill_total":"184500.00","diff":"0.00"}},
 "provenance":{"patient.dob":{"doc_id":"a1…","page":1,"confidence":0.97},"totals.gross":{"doc_id":"b7…","page":4,"confidence":0.93}},
 "etag":"draft-3","signoff":null}
```
**Edit** — `PUT` with `If-Match: draft-3` and JSON Patch limited to allowed paths:
```json
[{"op":"replace","path":"/bill_lines/14/qty","value":"1"},
 {"op":"remove","path":"/bill_lines/15"},
 {"op":"replace","path":"/totals/discounts","value":"4500.00"}]
```
Allowed path prefixes: `/bill_lines/*`, `/totals/*`, `/admission/diagnosis_codes`, `/admission/procedure_codes`, `/admission/treating_doctor`, `/patient/gender`. Forbidden (409 `path_not_editable`): `/patient/member_id`, `/patient/policy_number`, `/patient/dob`, `/admission/admitted_on`, `/admission/discharged_on`, `/claim_type` — these derive from the case record and must be corrected there (which re-triggers build). Wrong ETag → `412 precondition_failed`.
**Sign-off**
```json
POST /v1/cases/…/claim/signoff
{"decision":"approved","comment":"Verified against bill pp.2-4","acknowledged_warnings":["V10","V09"]}
→ 200 {"signoff_id":"…","draft_version":3}
→ 422 {"code":"warnings_not_acknowledged","missing":["V10"]}
→ 422 {"code":"draft_has_errors","errors":["V01"]}
```
**Submit**
```json
POST /v1/cases/…/claim/submit   {"late_filing_reason":null}
→ 202 {"outbox_id":"…","status":"pending","idempotency_key":"9b3c…","case_status":"submitted"}
→ 409 {"code":"signoff_stale","detail":"draft v4 created after sign-off on v3"}
→ 422 {"code":"filing_deadline_passed","detail":"reason required"}
```
**Submission status**
```json
{"status":"sent","attempts":2,"last_error":{"status":503,"at":"2026-10-06T10:15:31Z"},
 "insurer_claim_no":"IC-2026-004418","acknowledged_at":"2026-10-06T10:15:36Z",
 "ready_to_submit":{"ok":false,"reasons":[]}}
```

## 5. Build tasks
1. `claim_builder.start(case)`: check status `docs_complete`, no running job; POST to hospital-crew `/v1/jobs/claim-build` `{case_id, document_ids, route, config_versions}` (async, job id stored in Redis); status → `building_claim`.
2. Crew result endpoint (internal) validates `ClaimDraftIn` Pydantic model (strict) — crews never write DB directly (architecture rule); API persists as `claim_draft` v(n+1), `source='agent'`.
3. Deterministic validators `claim_validation/`:
   - V01 totals: `sum(lines) == totals.gross` (Decimal, exact);
   - V02 `gross - discounts == claimed`;
   - V03 line arithmetic `qty*unit_price == amount` (±0.01 rounding);
   - V04 each line has a `source_doc_id` belonging to the case and `source_page` within page count;
   - V05 dates within admission window; admission/discharge equal case fields;
   - V06 patient fields equal case/patient records (name fuzzy ≥ 90, DOB exact);
   - V07 ICD-10 regex + procedure code format;
   - V08 bill total from document parse equals claimed total (cross-doc);
   - V09 duplicate lines (same code+description+date) → warning;
   - V10 pre-auth amount vs claimed (cashless): claimed > preauth by >10% → warning `exceeds_preauth`;
   - V11 category sanity (room rent per day vs configured sanity ceiling → warning only);
   - V12 required documents referenced: each `documents[]` entry belongs to case and is `clean`.
   Errors block signoff; warnings need acknowledgement.
4. If validation has errors, auto-loop once: send errors back to crew for a repair attempt (max 2 repair rounds, PROPOSED), then surface to Officer for manual edit.
5. Officer edit endpoint: JSON Patch limited to allowed paths; each edit stored as new version `human_edit` with diff summary; audit `claim.edited` (field names only, not values containing PII).
6. Sign-off: Officer must acknowledge each warning (`acknowledged_warnings: [codes]`); creates `signoff` row bound to `draft_id`; any later edit invalidates (status `ready_for_review` → requires new sign-off). Builder and signer must differ? PROPOSED: no, but Officer cannot sign off a draft they edited if config `signoff.four_eyes=true` (default false).
7. Submission assembler `submission.assemble(draft, case)`: build `ClaimSubmission`; `documents[]` with presigned MinIO URLs (TTL 24 h, `ContentDisposition` attachment) plus `sha256`, `size_bytes`, `pages`, `parse_confidence`; attach `config_versions`; `contract_version="1.0"`; compute body hash.
8. Outbox pattern in one DB transaction at `submit`: insert `outbox` row (`idempotency_key = uuid5(claim_ref, draft_version)`), status → `submitted`, audit `claim.submitted`. Sender worker (`outbox/worker.py`, async loop plus Redis lock) polls due rows `FOR UPDATE SKIP LOCKED`, signs per 01-01 §3 using `claim_contract.signing.sign`, POSTs to insurer, handles responses:
   - 202 → store `insurer_claim_no`, status `acknowledged`, outbox `sent`;
   - 4xx (except 409 replay / 429) → `failed` terminal, surface to Officer with problem JSON;
   - 409 idempotency replay → treat as success;
   - 5xx/timeout/429 → exponential backoff 1s,4s,16s,60s,5m (jitter), max 8, then `dead` + notification.
9. Receiving callbacks (`/v1/insurer-callbacks/status|decisions|settlements`): verify HMAC + timestamp + idempotency (middleware from contract), insert `inbound_callback` (UNIQUE claim_ref, kind, sequence → ignore duplicates/old), map insurer status → hospital status via 01-02 §4.3 table, transition with guard, audit, SSE.
10. Decision handling: store decision JSON on case (`claim_case.decision jsonb` — migration 0013), compute `short_pay = claimed - approved`, set status; if partial/reject, create task for Officer (dispute/appeal out of scope; just displayed).
11. Settlement: record `settlement` (utr, amount, date) and move to `settled`, then auto-`closed` after N days (config).
12. Withdraw: calls insurer withdraw endpoint through outbox; allowed until decision.
13. Re-submission after insurer `needs_info` that requires corrected claim (not just query answers): new draft version → submit as supplement via `POST .../documents` + `claims` PUT? v1 contract only supports documents and query responses, so PROPOSED: corrections go through query responses; full resubmission not supported in v1 (document as limitation).
14. Pre-submission checklist component (API returns `ready_to_submit` boolean with reasons): docs complete, validation clean, signoff current, filing deadline not passed (warn), insurer reachable (`GET /v1/contract` ping).
15. Dead-letter UI data: `GET /v1/admin/outbox?status=dead`.
16. Metrics: `submission_latency_seconds`, `outbox_pending`, `outbox_dead`.

### 5.1 Step detail: validators (task 3)
Validator interface: `def Vnn(draft, case, docs, route) -> list[Finding]`; `Finding(code, severity: error|warning, field, message, ack_required: bool)`. Registry in `claim_validation/__init__.py` runs all, sorts by `(severity, code, field)`, computes `reconciliation`.

| Code | Severity | Condition / formula | Message template |
|---|---|---|---|
| V01 | error | `Σ line.amount != totals.gross` | "Line items total {sum} but gross is {gross} (diff {diff})." |
| V02 | error | `gross − discounts != claimed` | "Gross − discounts = {x}, claimed shows {claimed}." |
| V03 | error if |diff| > 0.01 | `qty × unit_price != amount` per line | "Line {n}: {qty} × {unit} = {calc}, shown {amount}." |
| V04 | error | `source_doc_id` ∉ case docs, or page > `page_count` | "Line {n} points to a document that is not part of this case." |
| V05 | error | `admitted_on/discharged_on` differ from case; any line date outside window ±1 d | "…" |
| V06 | error (DOB) / warning (name 80–89) / error (<80) | name fuzzy vs case patient; DOB exact | |
| V07 | warning | ICD-10 regex `^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$`; procedure code non-empty ≤ 12 chars | |
| V08 | error | `bill_total` extracted from bill doc ≠ `totals.gross` (tolerance 0.01) | "Bill document total {a} ≠ claim gross {b}." |
| V09 | warning, ack | same `(code, description, date, amount)` twice | |
| V10 | warning, ack | cashless and `claimed > preauth_amount × 1.10` | |
| V11 | warning | room rent per day > `sanity.room_per_day_max` (config, default 25,000) | |
| V12 | error | a `documents[]` item not in case, or `clean=false` (infected/quarantined) | |

Money arithmetic must use `Decimal` with `ROUND_HALF_UP`, never `float` (ruff rule from 01-05 applies).

### 5.2 Step detail: claim build job protocol (tasks 1-4)
```
API → crew:  POST /v1/jobs/claim-build
{ "job_id": "cb-…", "case_id": "…", "document_ids": ["…"], "route": {…}, "config_versions": {…},
  "callback": "http://hospital-api:8000/v1/internal/cases/{id}/claim/draft", "repair": null }

crew → API:  POST /v1/internal/cases/{id}/claim/draft   (svc-crew JWT)
{ "job_id": "cb-…", "payload": { … ClaimDraftIn … }, "provenance": {…},
  "model_info": {"alias":"extract-main","prompt_version":"cb-v4","trace_id":"…"}, "repair_round": 0 }

Repair request (from API after validation errors):
{ …same…, "repair": {"round": 1, "errors": [{"code":"V01","field":"totals.gross","message":"…"}], "previous_draft_version": 2} }
```
API behaviour on receipt: strict-parse → 422 `bad_draft_shape` (crew job marked failed, case returns to `docs_complete` with banner, Officer can retry); persist `claim_draft`; run validators; if errors and `repair_round < 2` → enqueue repair job and keep status `building_claim`; else status `ready_for_review` with errors visible. Idempotency on `job_id` (a duplicate result returns the previously stored version).

### 5.3 Step detail: sign-off invalidation
Triggers that call `signoff.invalidate(case, reason)`: any `PUT /claim`; any new draft version from agent; document deletion/reclassification affecting documents referenced by the draft; config republish with re-evaluate; route override changing claim type. Effect: `signoff.invalidated_at` set, case status → `ready_for_review` (from `submitted`? — **not allowed**; after submission edits are forbidden), audit `signoff.invalidated {reason}`, SSE `claim.signoff_invalidated`.

### 5.4 Step detail: assembling `ClaimSubmission` (task 7)
```python
def assemble(draft, case, docs, route, cfg_versions) -> ClaimSubmission:
    refs = []
    for d in docs_referenced(draft):  # only docs used by lines + required docs
        url = minio.presign_get(
            bucket="hospital-docs",
            key=d.object_key,
            expires=settings.presign_submit_ttl_s,
            response_headers={
                "response-content-disposition": f'attachment; filename="{d.safe_name}"'
            },
        )
        refs.append(
            DocumentRef(
                doc_id=d.id,
                doc_type=d.doc_type,
                filename=d.safe_name,
                sha256=d.sha256,
                size_bytes=d.size,
                download_url=url,
                parse_confidence=d.parse_confidence,
                pages=d.pages,
            )
        )
    sub = ClaimSubmission(
        contract_version="1.0",
        claim_ref=case.claim_ref,
        claim_type=route.pipeline,
        patient=Patient(
            **draft.payload["patient"], id_proof_hash=case.patient.id_proof_hash
        ),  # raw ID never included
        admission=Admission(**draft.payload["admission"], preauth_ref=case.preauth_ref),
        bill_lines=[BillLine(**l) for l in draft.payload["bill_lines"]],
        totals=ClaimTotals(**draft.payload["totals"]),
        documents=refs,
        config_versions=cfg_versions,
        submitted_at=clock.now(),
    )
    contract.validate_totals(sub)  # same rule as insurer: raises totals_mismatch
    return sub
```
Body size guard: serialized body must be < 200 KB (60 files × ~2 KB refs + lines); otherwise 422 `submission_too_large` (should not happen).

### 5.5 Step detail: callback receiver (task 9)
```python
@router.post("/v1/insurer-callbacks/status")
async def status_cb(req: Request, body: StatusUpdate, ctx=Depends(verify_hmac_idempotent)):
    async with db.begin():
        ins = await inbound.insert_if_new(
            body.claim_ref, "status", body.sequence, ctx.idem_key, body
        )
        if not ins.is_new:
            return JSONResponse(
                ins.stored_body,
                status_code=ins.stored_status,
                headers={"Idempotent-Replay": "true"},
            )
        case = await cases.by_claim_ref(body.claim_ref, lock=True)
        if case is None:
            raise Problem(404, "unknown_claim")
        if case.insurer_claim_no is None:
            case.insurer_claim_no = body.insurer_claim_no  # callback-before-ack case
        new_status = STATUS_MAP[body.status]  # table in 01-02 §4.3
        await transitions.apply_if_valid(case, new_status, actor="insurer", note=body.note)
        await audit.append(
            case.id, "callback.status", {"insurer_status": body.status, "seq": body.sequence}
        )
    await events.publish("case.status_changed", case.id, {"status": new_status})
```
`apply_if_valid`: out-of-order but valid-forward transitions are applied; backward/invalid transitions are recorded (`callback.ignored_invalid_transition`) and acknowledged with 200 so the insurer does not retry forever.

### 5.6 Backoff schedule (task 8)
| Attempt | Delay before | Jitter |
|---|---|---|
| 1 | 0 s | — |
| 2 | 1 s | ±20% |
| 3 | 4 s | ±20% |
| 4 | 16 s | ±20% |
| 5 | 60 s | ±20% |
| 6–8 | 300 s | ±20% |
After attempt 8 → `dead`; SSE `submission.dead`; Officer gets task; `POST /submission/retry` resets attempts to 0 with the **same** idempotency key (so a late success at the insurer is not duplicated).

### 5.7 Decision handling details (task 10)
`decisions` callback body: `Decision` (outcome, approved_amount, deductions[], reason_codes[], …). Computation: `short_pay = claimed − approved_amount` (Decimal). Hospital status map: `approve→approved`, `partial→partially_approved`, `reject→rejected`; `needs_info` is not a final outcome and arrives via queries. Persist `claim_case.decision`; create Officer task `review_decision` when `partial|reject`; render deductions table in UI (doc 10). If `approved_amount > claimed` → record anomaly `decision_exceeds_claim` and alert Officer (do not reject callback).

## 6. Key logic
```python
async def deliver(msg):
    body = canonical_json(msg.body)
    headers = sign(
        secret=settings.hosp_to_ins_secret,
        method=msg.method,
        path=msg.path,
        ts=utcnow_iso(),
        idem=str(msg.idempotency_key),
        body=body,
    )
    try:
        r = await http.request(
            msg.method, INSURER + msg.path, content=body, headers=headers, timeout=20
        )
    except (Timeout, ConnectError):
        return retry(msg)
    if r.status_code in (200, 202) or (r.status_code == 409 and r.headers.get("Idempotent-Replay")):
        return ok(msg, r)
    if r.status_code in (429,) or r.status_code >= 500:
        return retry(msg)
    return fail(msg, problem=r.json())
```
Sequence for callbacks: `inbound_callback` insert with ON CONFLICT DO NOTHING; if inserted → process in same tx; else return stored 200.

### 6.1 Worker loop
```python
async def run_worker():
    while True:
        async with db.begin():
            rows = await db.fetch("""SELECT * FROM outbox WHERE status IN ('pending') AND next_attempt_at <= now()
                                     ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 10""")
            for r in rows:
                await db.execute(
                    "UPDATE outbox SET status='sending', attempts=attempts+1 WHERE id=$1", r.id
                )
        await asyncio.gather(
            *(deliver(r) for r in rows)
        )  # each deliver() updates its own row in a new tx
        await asyncio.sleep(settings.outbox_poll_ms / 1000)


# crash recovery: rows stuck in 'sending' > 60 s are reset to 'pending' by a janitor (same idempotency key => safe)
```
Per-claim ordering: rows for one claim are delivered in `sequence` order — the query selects only the lowest-sequence unsent row per `case_id` (`DISTINCT ON (case_id)`), so `claim.documents` never overtakes `claim.submit`.

## 7. Config / env vars
`HOSP_INSURER_BASE_URL=https://insurer-api:8100` (or tpa-sim), `HOSP_KEY_ID`, `HOSP_TO_INS_HMAC_SECRET`, `INS_TO_HOSP_HMAC_SECRET`, `HOSP_OUTBOX_POLL_MS=1000`, `HOSP_PRESIGN_SUBMIT_TTL_S=86400`, `HOSP_CREW_URL=http://hospital-crew:8010`, config `signoff` domain (four_eyes, ack_required).

## 8. Error handling and edge cases
- Presigned URLs expire before insurer downloads (long outage): refresh endpoint (contract §6) regenerates; document.
- Insurer receives claim then hospital crashes before saving ack: replay with same key returns stored ack.
- Officer edits after submit: forbidden (409); use query response path.
- Clock skew > 300 s: insurer returns `stale_request`; worker logs and alerts.
- Money precision: Decimal everywhere; JSON as strings.
- Large documents (60 files): URL list only, body < 200 KB.
- Callback arrives before ack stored: handle by upserting insurer_claim_no from callback if absent.
- Filing deadline passed for reimbursement: require officer reason and mark `late_filing` flag in submission.
- Dead outbox: case stays `submitted`; banner in UI with retry.

### 8.1 Failure-mode table
| Failure | Detection | Result |
|---|---|---|
| Crew timeout (job > 5 min) | job TTL in Redis | case back to `docs_complete`, banner "Build timed out – retry"; audit `claim.build_failed` |
| Crew returns draft with unknown field | `extra="forbid"` Pydantic | 422, job failed; one automatic retry then manual |
| Two officers click submit simultaneously | `UNIQUE(idempotency_key)` + case row lock | second gets 409 `already_submitted` |
| Signed draft v3 but v4 exists | submit checks `signoff.draft_id == latest.id` | 409 `signoff_stale` |
| Insurer returns 422 `totals_mismatch` | 4xx terminal | `failed`; Officer sees problem JSON; case remains `submitted` but UI shows "rejected by receiver – edit & resubmit" → PROPOSED: transition back to `ready_for_review` with `submission_rejected` marker, allowed because nothing was accepted |
| MinIO down at assemble | presign raises | 503 `storage_unavailable` before any state change |
| HMAC secret rotated | 401 `invalid_signature` | outbox row `failed`; ops runbook R-secret; keys have two active ids (contract §3) |
| Insurer unreachable for hours | 5xx/timeouts | rows retry to `dead`; on recovery Officer retry reuses key |
| Callback with sequence gap (3 then 5) | detect gap | process 5, log `sequence_gap`; do not block |
| Duplicate settlement UTR | `UNIQUE(case_id, utr)` | ignored; replay response |

## 9. Tests
Validators V01-V12 (unit, positive/negative); builder happy path with fake crew; repair loop; sign-off invalidation on edit; outbox: success, timeout retry, 5xx retry, 4xx terminal, replay 409, dead-letter, worker crash mid-send (exactly-once effect via key); HMAC verify of callbacks (good/bad/stale/replay/out-of-order); status mapping table; contract tests against tpa-sim; concurrency (two officers submit simultaneously → one wins).

### 9.1 Validator test matrix (each row = positive and negative param)
| Code | Passing input | Failing input | Expect |
|---|---|---|---|
| V01 | lines 100.00+84.50 vs gross 184.50 | gross 184.51 | error V01, diff 0.01 |
| V02 | 184500−4500=180000 | claimed 180001 | error V02 |
| V03 | 4×5000.00=20000.00 | 4×5000.00=20000.05 | error; 20000.01 passes (±0.01) |
| V04 | page within count | page 9 of 6-page doc | error |
| V05 | discharge equals case | differs by 1 day | error |
| V06 | "Rahul Sharma" vs "Rahul  Sharma" | "Rohan Verma" | pass / error |
| V07 | `I21.0` | `I2` | pass / warning |
| V08 | bill 184500.00 | bill 184000.00 | pass / error |
| V09 | distinct lines | identical twin lines | warning ack_required |
| V10 | claimed ≤ 110% preauth | 120% | warning |
| V11 | room 5000/day | 40000/day | warning |
| V12 | clean docs | infected doc referenced | error |

### 9.2 Outbox and callback tests
- Kill worker between HTTP send and DB update: restart → resend with same key → tpa-sim returns replay → one insurer-side claim.
- Force 5xx ×3 then 202 → `attempts=4`, `sent`.
- 422 → `failed`, no retry, Officer notified.
- Clock skew +10 min → `stale_request`, alert.
- Callback matrix: valid, bad signature, stale timestamp, duplicate (same key), same sequence different body (409), older sequence (ignored), unknown claim (404), callback before ack.
- Ordering: enqueue `claim.documents` then `claim.submit` out of insertion order → delivered in `sequence` order.
- Load: 200 callbacks/s for 60 s with duplicates → no double transitions.

### 9.3 End-to-end script (tpa-sim)
1. Seed case with 12 clean docs → completeness complete.
2. `build` → fake crew returns draft with deliberate V01 error → repair round returns fixed draft.
3. Officer edits one line, acknowledges V09, signs off, submits.
4. tpa-sim: 503 once, then 202 ack; scripted status `verifying`, query round 1 (doc 07), decision `partial` (approved 90%), settlement.
5. Assert statuses and audit chain verify (`verify_chain`).

## 10. Acceptance criteria
- [ ] End-to-end with tpa-sim: build → signoff → submit → ack → status callbacks → decision → settlement.
- [ ] Killing the API mid-delivery never duplicates a submission at tpa-sim.
- [ ] Invalid totals can never be submitted.
- [ ] All state changes audited; contract tests green.

## 11. Dependencies
Docs 01-05; crew (doc 09) — fake until ready; contract 01-01/01-02/01-04; tpa-sim (Dev B, `04-shared-services`).

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/06-api-claim-builder-submission.md. First migration 0013 + validators with tests (tasks 3), then draft/sign-off flows, then the outbox worker and callback receivers. Use tpa-sim or a local fake insurer for integration tests.
