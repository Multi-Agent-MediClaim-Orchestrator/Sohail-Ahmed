# 03-02 — insurer-api: Claim Receipt, Signature Verification, Idempotency (2.2.1)

Owner: Dev B. Status: PROPOSED. Container: `insurer-api` (FastAPI, port 8100).
Depends on: `01-api-contract-v1.md`, `02-data-models-and-enums.md`, `04-audit-hash-chain.md`, `03-dev-B-insurer/01-insurer-db.md`.

## 1. Goal
Be the front door for hospital → insurer traffic: authenticate (HMAC), deduplicate (idempotency), validate the `ClaimSubmission`, persist it atomically, fetch documents, acknowledge in under 2 seconds, and hand the case to the verification flow asynchronously.

## 2. Inputs / Outputs
- Input: `POST /v1/hospital-api/claims` with `ClaimSubmission` JSON, signed headers; also supplementary `documents`, `withdraw` and `queries/*/responses` (the latter two handled in 05).
- Output: `202 Acknowledgement {insurer_claim_no, received_at, status:"received", sequence:1}`; rows in `core.claim_case`, `claim_document`, `bill_line`, audit events, an n8n trigger via webhook, and an outbox status callback to the hospital.

## 3. Data model
Uses tables from 01-insurer-db: `claim_case`, `claim_document`, `bill_line`, `network_hospital`, `ops.idempotency_record`, `ops.outbox`, `audit.*`. No new tables.

## 4. API/endpoints

### 4.1 `POST /v1/hospital-api/claims`
Request headers: see contract §3. Body: `ClaimSubmission`.

Success (202):
```json
{"insurer_claim_no":"IC-2026-000123","claim_ref":"HC-2026-000045","status":"received",
 "received_at":"2026-10-06T10:15:31Z","sequence":1,"contract_version":"1.0"}
```
Replay (same key + body): same body, header `Idempotent-Replay: true`.
Duplicate claim_ref with different idempotency key and identical `submission_hash` → return stored ack (200 with `Idempotent-Replay: true`); different hash → 409 `idempotency_conflict` (PROPOSED: hospital must use supplement endpoint to change data).

Errors: 401 `invalid_signature|stale_request`, 400 `unsupported_version`, 422 `validation_error|totals_mismatch`, 403 `hospital_blacklisted` (PROPOSED code, add to contract errors via PR), 404 none, 429 `rate_limited`.

### 4.2 `GET /v1/hospital-api/claims/{claim_ref}`
Returns `StatusUpdate` using cross-side mapping in 01-02 §4.3. Only the hospital that owns the claim (key id → hospital) may read; otherwise 404 (not 403, to avoid leaking existence).

### 4.3 `POST /v1/hospital-api/claims/{claim_ref}/documents`
Body `{documents: [DocumentRef,...], reason: "query_response"|"voluntary", query_id?: UUID}`. Allowed when case status in `received, verifying, needs_info, ready_for_decision`; otherwise 409 `invalid_transition`. Creates new `claim_document` rows (earlier versions of same `doc_type` get `superseded_by`), triggers document fetch and re-verification when `reason=query_response`.

### 4.4 `GET /v1/health`, `GET /v1/contract`
Static; `/v1/contract` returns `{"supported":["1.0"]}`.

### 4.5 Full request/response examples

**Request** (abbreviated body; headers shown in full):
```http
POST /v1/hospital-api/claims HTTP/1.1
Host: insurer-api:8100
Content-Type: application/json
X-Contract-Version: 1.0
X-Key-Id: hosp-001
X-Timestamp: 2026-10-06T10:15:30Z
X-Idempotency-Key: 0192b7f4-3c1e-7a55-9d0b-5f3a1c2e8d41
X-Trace-Id: tr_01hq...
X-Signature: 4f1Zk...base64...=

{"contract_version":"1.0","claim_ref":"HC-2026-000045","claim_type":"cashless",
 "patient":{"full_name":"Ravi Kumar S","dob":"1984-03-11","gender":"M","member_id":"M-004512","policy_number":"HP-2025-001881","id_proof_hash":"9a1c..."},
 "admission":{"admission_type":"emergency","admitted_on":"2026-09-28","discharged_on":"2026-10-02",
   "diagnosis_codes":["K35.8"],"procedure_codes":["0DTJ4ZZ"],"treating_doctor":"Dr A Rao","hospital_id":"HOSP-BLR-014","preauth_ref":"PA-77812"},
 "bill_lines":[{"code":"RM-GEN","description":"General ward","category":"room","qty":"4","unit_price":{"amount":"3000.00","currency":"INR"},"amount":{"amount":"12000.00","currency":"INR"},"source_doc_id":"0192...","source_page":2}],
 "totals":{"gross":{"amount":"186500.00","currency":"INR"},"discounts":{"amount":"0.00","currency":"INR"},"claimed":{"amount":"186500.00","currency":"INR"}},
 "documents":[{"doc_id":"0192...","doc_type":"discharge_summary","filename":"ds.pdf","sha256":"ab12...","size_bytes":220114,"download_url":"https://minio:9000/hospital-docs/...","parse_confidence":0.93,"pages":3}],
 "config_versions":{"doc_requirements":3,"deadlines":1,"router_rules":2,"confidence_gates":1},
 "submitted_at":"2026-10-06T10:15:29Z"}
```

**Error examples**
```json
// 422 totals_mismatch
{"type":"https://claims.local/errors/totals_mismatch","title":"Totals mismatch","status":422,"code":"totals_mismatch",
 "detail":"Sum of bill_lines (186000.00) differs from totals.gross (186500.00)",
 "errors":[{"field":"totals.gross","message":"expected 186000.00"}],"trace_id":"tr_01hq..."}
// 403 hospital_blacklisted
{"type":"https://claims.local/errors/hospital_blacklisted","title":"Hospital not permitted","status":403,"code":"hospital_blacklisted",
 "detail":"Hospital HOSP-XYZ is not currently permitted to submit claims","trace_id":"tr_01hq..."}
// 409 request_in_progress
{"type":"https://claims.local/errors/request_in_progress","title":"Request in progress","status":409,"code":"request_in_progress","detail":"Retry after 2 seconds"}
```

### 4.6 Validation rule catalogue (`validators/submission.py`)
| # | Rule | Failure → code | Notes |
|---|---|---|---|
| V1 | `contract_version` major == 1 | 400 `unsupported_version` | minor ignored if <= server minor |
| V2 | `Σ bill_lines.amount == totals.gross` (exact Decimal) | 422 `totals_mismatch` | |
| V3 | `claimed == gross - discounts` | 422 `totals_mismatch` | |
| V4 | each line `qty × unit_price == amount` (tolerance 0.01) | 422 `validation_error` (field `bill_lines[i]`) | rounding rule: half-up to 2dp |
| V5 | `discharged_on >= admitted_on`, neither in the future, stay <= 180 days | 422 | |
| V6 | ICD-10 regex `^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$` | warning in audit payload only | unknown code is not fatal |
| V7 | at most 200 documents, total declared size <= 500 MB, each <= 50 MB | 422 | |
| V8 | every `bill_lines.source_doc_id` exists in `documents` | 422 | |
| V9 | hospital row `active` and `network_status != 'blacklisted'` | 403 `hospital_blacklisted` | |
| V10 | `admission.hospital_id` equals the hospital resolved from `X-Key-Id` | 403 `hospital_mismatch` (PROPOSED code) | stops a hospital submitting under another's id |
| V11 | `preauth_ref` present when `claim_type=cashless` and `admission_type=planned` | warning finding recorded for completeness step | not fatal at the door |
| V12 | at most 2,000 bill lines | 422 | |
| V13 | `download_url` host in allow-list `INS_ALLOWED_DOC_HOSTS` (SSRF guard) | 422 | URLs to metadata endpoints / internal services refused |
| V14 | duplicate `doc_id` within submission | 422 | |

### 4.7 Supplement endpoint example (`POST /v1/hospital-api/claims/{claim_ref}/documents`)
```json
{"reason":"query_response","query_id":"0192b800-...","documents":[
  {"doc_id":"0192b801-...","doc_type":"discharge_summary","filename":"ds_signed.pdf","sha256":"cd34...","size_bytes":240511,
   "download_url":"https://minio:9000/hospital-docs/...","parse_confidence":0.96,"pages":3}]}
```
Response 202 `{"accepted":1,"superseded":["0192b7f4-..."],"sequence":4}`. Rules: documents with `doc_type` equal to an existing non-superseded document supersede it only if `reason=query_response` and an open query lists that type; otherwise they are added as additional documents. Voluntary supplements are accepted but do not close any query.

### 4.8 Withdraw (`POST /v1/hospital-api/claims/{claim_ref}/withdraw`)
Allowed when status in `received, verifying, needs_info, ready_for_decision`; moves to `closed` with `closure_reason='withdrawn_by_hospital'`, cancels open queries (`status=closed`), cancels pending outbox rows except the final status callback, and emits audit `claim.withdrawn`. After `awaiting_approval` or a final decision it returns 409 `invalid_transition`.

## 5. Build tasks
1. Package layout:
```
insurer/api/app/
  main.py  config.py  deps.py
  routers/hospital_api.py   (this doc)  routers/internal.py (n8n/crew)  routers/admin.py
  services/receipt.py  services/docs_fetch.py  services/outbox.py
  security/hmac_auth.py  security/idempotency.py  security/keycloak.py
  schemas/ (re-export claim_contract.models)
```
2. `security/hmac_auth.py`: FastAPI dependency `verify_hospital_request` reading raw body (use `Request.body()` once and cache in `request.state.raw_body`), computing canonical string via `claim_contract.signing.verify`, resolving `X-Key-Id` → `network_hospital.hmac_key_id`; secret loaded from env `INS_HMAC_SECRETS` JSON `{key_id:[secret_current, secret_previous]}` to support rotation. Constant-time compare via `hmac.compare_digest`.
3. `security/idempotency.py`: dependency/decorator; Redis `SET key NX EX 86400` lock then DB `ops.idempotency_record`. Flow in §6.2.
4. Rate limiter: `slowapi`-style Redis token bucket 100 req/min per key id; return 429 with `Retry-After`.
5. `services/receipt.py::receive_claim()`; one transaction (§6.1).
6. Validation layer `validators/submission.py` beyond Pydantic: totals reconcile, date sanity, ICD-10 regex, hospital network/blacklist status, duplicate detection, contract version.
7. Insert audit event `claim.received` via `claim_contract.audit.append`.
8. Enqueue outbox callback `status` (received→"acknowledged" per mapping) — after commit.
9. Trigger verification: `POST {INS_N8N_URL}/webhook/verification-start` with `{case_id}` using service token; failure to trigger must not fail the receipt — write `ops.outbox`-like retry row `kind=n8n_trigger` (PROPOSED reuse outbox with endpoint prefix `n8n:`).
10. `services/docs_fetch.py`: background task (Arq on Redis) `fetch_document(doc_id)` downloads presigned URL with httpx (timeout 30 s, size cap 50 MB), verifies sha256 and size, uploads to MinIO `insurer-docs/{case_id}/{doc_id}`, scans with ClamAV (insurer also scans, defence in depth), updates `fetch_status`. Retries 5x with backoff; hash mismatch → `hash_mismatch` and finding `doc.hash_mismatch` severity blocker.
11. `GET` status and supplement endpoints.
12. Structured logging with `trace_id` (from `X-Trace-Id` header or generated) and request id; never log bodies.
13. OpenAPI export check: `make contract-test` runs schemathesis against this router.
14. `hospital-sim` fixture (`tests/fixtures/hospital_sim.py`) generating signed requests from the 5 valid / 10 invalid fixtures in contract.
15. Add `claim_document.source_url TEXT` (store the presigned URL encrypted at rest using Fernet key `INS_URL_ENC_KEY`, since it is a bearer credential) in migration `0008`; never log it.
16. Allow-list check `assert_allowed_host()` (`INS_ALLOWED_DOC_HOSTS=minio:9000,hospital-minio:9000`) and a unit test with 10 SSRF payloads (IPv4/IPv6 literals, decimal IPs, redirects, `localhost`, `file://`).
17. Arq worker entrypoint `insurer/api/app/worker.py` with functions `fetch_document`, `fetch_documents`, `start_verification`, `send_outbox`, and cron `purge_ops`, `sla_tick`.
18. Prometheus counters: `receipt_total{result}`, `receipt_latency_seconds`, `idempotent_replays_total`, `doc_fetch_total{status}`, `outbox_pending`.
19. Write `docs/implementation/03-dev-B-insurer/assets/receipt-sequence.md` (mermaid sequence of §6.6) and keep it in sync.

## 6. Key logic / pseudocode

### 6.1 receive_claim
```python
async def receive_claim(
    req: ClaimSubmission, hospital: NetworkHospital, idem_key: UUID, db, now
) -> Ack:
    validate_submission(req, hospital)  # raises ProblemError
    h = sha256_canonical(req)
    async with db.begin():
        existing = await claims.get_by_ref(hospital.id, req.claim_ref, for_update=True)
        if existing:
            if existing.submission_hash == h:
                return ack_from(existing)  # replay
            raise Conflict("idempotency_conflict")
        policy = await policies.find(req.patient.policy_number)  # may be None → identity step flags
        member = await members.find_by_member_id(req.patient.member_id)
        case = ClaimCase(
            id=uuid7(),
            insurer_claim_no=await next_claim_no(db),
            hospital_claim_ref=req.claim_ref,
            hospital_id=hospital.id,
            claim_type=req.claim_type,
            admission_type=req.admission.admission_type,
            status="received",
            policy_id=policy and policy.id,
            member_id=member and member.id,
            claimed_amount=req.totals.claimed.amount,
            contract_version=req.contract_version,
            submission=req.model_dump(mode="json"),
            submission_hash=h,
            received_at=now,
            sla_due_at=await sla_for(req.claim_type, req.admission.admission_type, now),
            priority=priority_for(req),
        )
        db.add(case)
        db.add_all(bill_lines(case, req))
        db.add_all(documents(case, req))
        await audit.append(
            db,
            case.id,
            actor=("system", "insurer-api"),
            type="claim.received",
            payload=redact(
                {
                    "hospital": hospital.hospital_code,
                    "claimed": str(case.claimed_amount),
                    "docs": len(req.documents),
                }
            ),
            cfg=current_config_versions(case),
        )
        await outbox.enqueue(db, case, "status", status_update(case, "acknowledged"))
        await jobs.enqueue_after_commit("fetch_documents", case.id)
        await jobs.enqueue_after_commit("start_verification", case.id)
    return ack_from(case)
```
Priority PROPOSED: emergency=1, cashless planned=2, reimbursement=3; +bump if `claimed > T_auto`.
SLA PROPOSED: cashless emergency 4 h to first decision recommendation, cashless planned 8 h, reimbursement 5 working days; configurable in `query_policy`/`thresholds` extension key `sla_hours`.

### 6.2 Idempotency middleware
```python
async def idempotent(request, key_id, idem_key, body_hash):
    rec = await redis.get(f"idem:{key_id}:{idem_key}") or await db_lookup(key_id, idem_key)
    if rec:
        if rec.request_hash != body_hash:
            raise Conflict("idempotency_conflict")
        return Replay(rec.response_status, rec.response_body)
    got = await redis.set(f"idemlock:{key_id}:{idem_key}", "1", nx=True, ex=30)
    if not got:
        raise HTTPException(409, "request_in_progress")  # retry-after 2s
    try:
        resp = await call_handler()
        await store(key_id, idem_key, body_hash, resp)
        return resp
    finally:
        await redis.delete(f"idemlock:...")
```
Store only 2xx and 4xx deterministic responses; never 5xx.

### 6.3 Outbox sender (shared with 05 and 06)
Worker loop `SELECT ... FROM ops.outbox WHERE status='pending' AND next_attempt_at<=now() ORDER BY seq FOR UPDATE SKIP LOCKED LIMIT 20`; sign request with INS→HOSP secret; per-claim ordering ensured by only sending the lowest pending `seq` per case (`DISTINCT ON (case_id)`). Backoff 1s,4s,16s,60s,300s up to 8 attempts then `dead` and raise admin alert (`ops` dashboard panel in 10-ui).

### 6.4 HMAC dependency (reference implementation)
```python
# insurer/api/app/security/hmac_auth.py
async def verify_hospital_request(
    request: Request, db=Depends(get_db), settings=Depends(get_settings)
) -> AuthedHospital:
    h = request.headers
    try:
        key_id, ts, idem, sig = (
            h["X-Key-Id"],
            h["X-Timestamp"],
            h.get("X-Idempotency-Key", ""),
            h["X-Signature"],
        )
    except KeyError as e:
        raise ProblemError(401, "invalid_signature", f"missing header {e.args[0]}")
    skew = abs((now() - parse_iso(ts)).total_seconds())
    if skew > settings.clock_skew_seconds:
        log.warning("stale_request", key_id=key_id, skew=skew)
        raise ProblemError(401, "stale_request", "timestamp outside allowed window")
    raw = await request.body()
    request.state.raw_body = raw
    if request.method in ("POST", "PUT") and not idem:
        raise ProblemError(422, "validation_error", "X-Idempotency-Key required")
    secrets_for_key = settings.hmac_secrets.get(key_id)  # [current, previous]
    if not secrets_for_key:
        raise ProblemError(401, "invalid_signature", "unknown key")
    canonical = canonical_string(
        request.method, request.url.path_with_query, ts, idem, sha256_hex(raw)
    )
    if not any(hmac.compare_digest(sig, sign(s, canonical)) for s in secrets_for_key):
        raise ProblemError(401, "invalid_signature", "signature mismatch")
    hospital = await hospitals.by_key_id(db, key_id)
    return AuthedHospital(
        hospital=hospital, key_id=key_id, idem_key=idem, body_hash=sha256_hex(raw)
    )
```
`invalid_signature` responses are generic on purpose (no hint which part failed) while the log line carries the specific reason.

### 6.5 Document fetch job (Arq)
```python
async def fetch_document(ctx, doc_id: UUID):
    doc = await docs.get(doc_id)
    if doc.fetch_status == "fetched":
        return
    url = doc.source_url  # stored in claim_document.source_url (add column, migration 0008)
    assert_allowed_host(url)  # SSRF guard V13
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        async with c.stream("GET", url) as r:
            if r.status_code in (403, 404):
                return await mark(doc, "failed", "doc.url_expired")
            r.raise_for_status()
            h, size, tmp = hashlib.sha256(), 0, TemporaryFile()
            async for chunk in r.aiter_bytes(65536):
                size += len(chunk)
                if size > settings.max_doc_bytes:
                    return await mark(doc, "failed", "doc.too_large")
                h.update(chunk)
                tmp.write(chunk)
    if h.hexdigest() != doc.sha256 or size != doc.size_bytes:
        return await mark(doc, "hash_mismatch", "doc.hash_mismatch")
    tmp.seek(0)
    if not await clamav.scan(tmp):
        return await mark(doc, "failed", "doc.virus_found")
    tmp.seek(0)
    key = f"{doc.case_id}/{doc.id}"
    await minio.put_object("insurer-docs", key, tmp, size)
    await docs.update(doc.id, fetch_status="fetched", object_key=key)
    await audit.append(
        ..., type="doc.fetched", payload={"doc_id": str(doc.id), "sha256": doc.sha256}
    )
```
Retry policy: Arq `max_tries=5`, `retry_delay` 5 s, 20 s, 60 s, 300 s, 900 s on network errors and 5xx only; 4xx and hash/virus failures are terminal. When all docs reach a terminal state, the job emits the `documents_ready` signal that n8n waits on (webhook `POST {n8n}/webhook/documents-ready`).

### 6.6 Request lifecycle summary
```
hospital → [rate limit] → [HMAC verify] → [idempotency lookup/lock] → [Pydantic parse] → [validators V1-V14]
        → [txn: insert case, lines, docs, audit, outbox] → [commit] → enqueue fetch + n8n trigger → 202
```
The 202 is returned before documents are fetched; the target is p95 < 2 s which leaves the fetch outside the critical path.

### 6.7 Status mapping function (used for GET and callbacks)
```python
INSURER_TO_HOSPITAL = {
    "received": "acknowledged",
    "verifying": "acknowledged",
    "needs_info": "under_query",
    "ready_for_decision": "acknowledged",
    "awaiting_approval": "acknowledged",
    "escalated": "under_query",
    "approved": "approved",
    "partially_approved": "partially_approved",
    "rejected": "rejected",
    "settled": "settled",
    "closed": "closed",
}


def status_update(case, hospital_status=None) -> StatusUpdate:
    return StatusUpdate(
        claim_ref=case.hospital_claim_ref,
        insurer_claim_no=case.insurer_claim_no,
        status=hospital_status or INSURER_TO_HOSPITAL[case.status],
        sequence=case.last_callback_seq + 1,
        updated_at=case.updated_at,
        detail=None,
    )
```
Internal statuses `ready_for_decision` and `awaiting_approval` are never leaked; only the mapped value crosses the boundary.

## 7. Config / env vars
`INS_HMAC_SECRETS` (JSON), `INS_INS_TO_HOSP_HMAC_SECRET`, `INS_REDIS_URL`, `INS_N8N_URL`, `INS_N8N_SERVICE_TOKEN`, `INS_MINIO_*`, `INS_CLAMAV_HOST`, `INS_MAX_DOC_BYTES=52428800`, `INS_RATE_LIMIT_PER_MIN=100`, `INS_HOSPITAL_CALLBACK_BASE` (resolved per hospital row; PROPOSED add column `callback_base_url` to `network_hospital` in migration 0002), `INS_CLOCK_SKEW_SECONDS=300`.

## 8. Error handling and edge cases
- Body read once; signature computed over raw bytes, not re-serialised JSON.
- Clock skew: reject > 300 s; log skew value for diagnosis.
- Two simultaneous identical submissions: unique index plus `FOR UPDATE` plus Redis lock ensures one case.
- Unknown policy number: still accept (202); identity step flags `policy_not_found` blocker → `needs_info`. Rejecting at the door would violate "fixable before fatal".
- Hospital blacklisted → 403, audit `claim.refused`.
- Presigned URL expired when fetching: mark `failed`, finding `doc.url_expired`, call hospital refresh (contract §6) — if unavailable, raise query `missing_document`/`illegible_document` category automatically via 05.
- Document count > 200 or > 500 MB total → 422 `validation_error`.
- Contract minor newer than supported (e.g. 1.3): accept if major same, ignore unknown fields only if `extra=ignore` flag on receiving models (PROPOSED: models are `forbid`, so reject unknown fields with 422 and a clear message; minor bumps are coordinated).
- DB down: return 503 `internal_error` with `Retry-After`, never partial writes.
- Supplement for withdrawn/closed case → 409.

### 8.1 Edge-case table
| Situation | Behaviour |
|---|---|
| Same `claim_ref` resubmitted with new idempotency key, same body | 200 replay of stored ack |
| Same `claim_ref`, changed totals | 409 `idempotency_conflict`; hospital must use supplement or withdraw+resubmit with new ref |
| Idempotency key reused on a different path | 409 `idempotency_conflict` (key scoped to key_id; request hash includes path) |
| Redis down | Fall back to DB idempotency table only; lock via `pg_advisory_xact_lock(hashtext(key))`; log degraded mode |
| MinIO down at fetch time | Job retries; case proceeds to `verifying` only when `documents_ready` fires; after 1 h of failure raise admin alert and keep case `received` |
| n8n webhook returns 500 | Retry rows with backoff; admin panel shows "verification not started" count |
| Hospital callback URL unreachable | Outbox backoff; case still progresses; UI shows delivery failures |
| Body is valid JSON but not UTF-8 NFC | Accepted; hash computed on raw bytes |
| Clock of hospital ahead by 6 min | 401 `stale_request`; log includes skew so ops can fix NTP |
| Key rotation in progress | Both current and previous secrets accepted until previous is removed from env |
| Replay of an old, valid signed request after 5 min | Rejected by timestamp even if idempotency record expired |
| Content-Length mismatch / chunked > 2 MB | 413 `payload_too_large` (PROPOSED code) |
| Extra unknown JSON field | 422 listing the field |
| `claim_type=reimbursement` with `preauth_ref` | accepted; ignored |

## 9. Tests
Unit: signature vectors; stale; idempotency matrix (same/different body, concurrent); validators (each rule); priority/SLA derivation.
Integration (testcontainers): full receive → rows created, audit chain valid, outbox row present; replay returns identical body; blacklisted hospital; unknown policy accepted; document fetch with MinIO + a local file server serving presigned URL, hash mismatch case, ClamAV EICAR rejection.
Contract: schemathesis against OpenAPI for all hospital-api routes.
Load: 50 concurrent submissions (k6/locust) p95 < 2 s ack on dev machine.
Chaos: kill n8n during trigger → retry row eventually delivered.

### 9.1 Test matrix
| ID | Scenario | Setup | Expected |
|---|---|---|---|
| R-01 | Happy path | valid fixture #1 | 202; 1 case, N lines, M docs; audit seq 1; outbox 1 row; n8n trigger job queued |
| R-02 | Exact replay | send R-01 twice | second: same body, `Idempotent-Replay: true`, 1 case only |
| R-03 | Conflict | same key, body changed | 409 `idempotency_conflict` |
| R-04 | Concurrent identical | 10 parallel requests | exactly one case; others replay or get `request_in_progress` then replay |
| R-05 | Tampered body | flip 1 byte after signing | 401 `invalid_signature` |
| R-06 | Stale timestamp | ts = now − 400 s | 401 `stale_request` |
| R-07 | Unknown key id | `X-Key-Id: x` | 401 `invalid_signature` |
| R-08 | Previous secret | sign with previous secret | 202 |
| R-09 | Totals mismatch | gross off by 0.01 | 422 `totals_mismatch` |
| R-10 | Line arithmetic | qty×price ≠ amount | 422 |
| R-11 | Future discharge date | +1 day | 422 |
| R-12 | Blacklisted hospital | seed hospital #12 | 403, audit `claim.refused` |
| R-13 | Hospital mismatch | body hospital_id ≠ key's hospital | 403 `hospital_mismatch` |
| R-14 | Unknown policy | policy number not in master | 202; later identity flags |
| R-15 | SSRF URL | `download_url=http://169.254.169.254/` | 422 |
| R-16 | Doc hash mismatch | file served differs | `hash_mismatch`; finding raised |
| R-17 | EICAR file | ClamAV flags | `failed`, finding `doc.virus_found` |
| R-18 | Expired URL | 403 from MinIO | `doc.url_expired`; refresh attempted |
| R-19 | Supplement allowed | case `needs_info` | 202; superseded set |
| R-20 | Supplement after approval | status `approved` | 409 `invalid_transition` |
| R-21 | Withdraw | status `verifying` | 200; case closed; queries closed |
| R-22 | Read other hospital's claim | key B reads claim of A | 404 |
| R-23 | Rate limit | 101 requests in a minute | 429 + `Retry-After` |
| R-24 | Redis outage | stop Redis | still 202; degraded log |
| R-25 | n8n outage | stop n8n | 202; trigger retried after restart |
| R-26 | Oversize body | 3 MB | 413 |
| R-27 | Log hygiene | run R-01..R-10 | grep logs for patient name/ID hash → zero hits |

## 10. Acceptance criteria
- [ ] All hospital-sim fixtures behave as documented (valid → 202, invalid → expected code).
- [ ] Replay and conflict semantics proven by tests.
- [ ] Ack p95 < 2 s with 10 concurrent claims.
- [ ] Audit event `claim.received` present and chain verifies.
- [ ] Documents fetched, hash verified, scanned, stored in `insurer-docs`.
- [ ] No request body or ID number appears in logs (grep test).

## 11. Dependencies
01-insurer-db, contract library (signing, idempotency helpers, audit), redis/minio/clamav (Dev A infra). Needed by 03 (verification start), 05 (responses endpoint shares auth), 06.

## 12. Claude Code kickoff prompt
> Read the shared-contract docs and docs/implementation/03-dev-B-insurer/02-api-claim-receipt.md. Plan, then implement build tasks 1-14 under insurer/api/. Use the hospital-sim fixtures for tests; do not call a real hospital. Run the section 9 tests and report against section 10.
