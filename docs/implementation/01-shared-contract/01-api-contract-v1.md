# 01-01 — Hospital ↔ Insurer REST Contract v1 (with v1.1 additions)

Status: PROPOSED. Joint ownership (both developers approve every change). OpenAPI source: `contract/openapi/claims-v1.yaml`. The architecture file calls this "section 9".
Version: **1.0** is the frozen baseline at the end of Phase 0. **1.1** (section 12) adds `X-Journey-Id` and the document-URL refresh endpoint; both are additive/minor and PROPOSED.

---

## 1. Goal
The only channel between the Hospital system and the Insurer/TPA system. It must be:
- **Versioned** — every request names the contract version; breaking changes get a new major path.
- **Signed** — HMAC-SHA256 per direction, with replay protection by timestamp.
- **Idempotent** — every mutating call carries an idempotency key and can be retried safely.
- **Resumable** — at-least-once delivery with sequence numbers, outbox on the sender, dead-letter on repeated failure.
- **Opaque to internals** — neither side learns the other's DB schema, internal statuses, or model details.

## 2. Inputs / Outputs
| Direction | Input | Output |
|---|---|---|
| Hospital → Insurer | `ClaimSubmission`, `DocumentRef[]`, `QueryResponse`, withdrawal | `Acknowledgement`, `StatusUpdate`, `Query[]` |
| Insurer → Hospital | `StatusUpdate`, `Query`, `Decision`, `SettlementNotice`, document-URL refresh request | `204`/`200` receipts |

Models are defined in `01-02-data-models-and-enums.md` and are not repeated here except where an example needs them.

## 3. Data model (transport-level objects)

### 3.1 Request envelope headers (all requests)
| Header | Required | Format | Notes |
|---|---|---|---|
| `X-Contract-Version` | yes | `MAJOR.MINOR` e.g. `1.0` | server rejects unsupported major `400 unsupported_version` |
| `X-Key-Id` | yes | `hosp-001` / `ins-001` | identifies which secret to use; two active ids allowed during rotation |
| `X-Timestamp` | yes | RFC 3339 UTC, seconds precision, `Z` suffix | skew limit ±300 s |
| `X-Idempotency-Key` | POST/PUT/PATCH/DELETE | UUID (v7 preferred) | GET/HEAD must omit; if omitted, canonical uses empty string |
| `X-Signature` | yes | base64 (standard alphabet, padded) of HMAC-SHA256 | see section 4 |
| `X-Request-Id` | no | UUID | echoed in response and error `trace_id` |
| `X-Journey-Id` | no (v1.1, PROPOSED) | UUID | see section 12 |
| `Content-Type` | when body present | `application/json; charset=utf-8` | |

### 3.2 Response headers
| Header | When | Meaning |
|---|---|---|
| `X-Contract-Version` | always | version the server handled the request as |
| `X-Request-Id` | always | echo or generated |
| `Idempotent-Replay: true` | replayed response | body/status are stored from the first execution |
| `Retry-After` | 429, 503 | seconds |
| `X-RateLimit-Limit/Remaining/Reset` | always | per key id |

### 3.3 Pagination envelope (list endpoints)
```json
{ "items": [ ... ], "next_cursor": "opaque-string-or-null", "limit": 50 }
```
Query params: `limit` (1-200, default 50), `cursor`.

## 4. Authentication — HMAC signing

### 4.1 Canonical string
```
canonical = METHOD + "\n" + PATH_WITH_QUERY + "\n" + X-Timestamp + "\n" + X-Idempotency-Key + "\n" + sha256_hex(body_bytes)
```
- `METHOD` upper-case.
- `PATH_WITH_QUERY` exactly as sent on the wire (percent-encoding preserved, query params in the sent order). Both sides sign the raw request target, never a re-serialised one.
- `X-Idempotency-Key` is the empty string when absent (GET).
- `body_bytes` are the raw bytes; empty body hashes to `e3b0c442...b855`.
- `X-Signature = base64( HMAC_SHA256(secret, canonical.encode("utf-8")) )`.

### 4.2 Verification rules (order matters; first failure wins)
1. `X-Contract-Version` major supported, else `400 unsupported_version`.
2. Required headers present, else `401 invalid_signature` (never reveal which was missing).
3. `X-Key-Id` known and active, else `401 invalid_signature`.
4. `|now_utc − X-Timestamp| ≤ 300 s`, else `401 stale_request`.
5. Recompute signature over raw body; compare with `hmac.compare_digest`; mismatch → `401 invalid_signature`.
6. Idempotency check (section 5).
7. Rate limit (section 9).

### 4.3 Secrets and rotation
- Two secrets, one per direction: `HOSP_TO_INS_HMAC_SECRET` (hospital signs, insurer verifies) and `INS_TO_HOSP_HMAC_SECRET`.
- Each secret has a key id (`hosp-001`, `hosp-002`). Rotation: add new id as active on the verifier → switch signer → after 24 h retire the old id. Verifier keeps a list `[{key_id, secret, status: active|retiring|retired}]`.
- Secrets ≥ 32 random bytes, base64 in env; never logged. Dev secrets in `.env.example` are clearly fake.

### 4.4 Test vectors (computed; every implementation must reproduce them)
Secret (UTF-8 bytes): `test-secret-hosp-to-ins-0001`

**Vector V1 — POST with body**
```
METHOD:           POST
PATH:             /v1/hospital-api/claims
X-Timestamp:      2026-10-06T10:15:30Z
X-Idempotency-Key:0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b
BODY:             {"claim_ref":"HC-2026-000001"}
sha256_hex(body): e0678c4517da832a54ac8bbf5cf98c283515dc769ef4d1dadf9e413790c5e3e8
canonical:        "POST\n/v1/hospital-api/claims\n2026-10-06T10:15:30Z\n0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b\ne0678c4517da832a54ac8bbf5cf98c283515dc769ef4d1dadf9e413790c5e3e8"
X-Signature:      JS6xvXXtuHpT2sltvMWlI/9EhupyXRohlMY+DXjc7qw=
```

**Vector V2 — GET with query, no idempotency key, empty body**
```
METHOD:           GET
PATH:             /v1/hospital-api/claims/HC-2026-000001?include=queries
X-Timestamp:      2026-10-06T10:16:00Z
X-Idempotency-Key:(empty)
sha256_hex(body): e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
canonical:        "GET\n/v1/hospital-api/claims/HC-2026-000001?include=queries\n2026-10-06T10:16:00Z\n\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
X-Signature:      rSj5CoHCq3nAPK4xBnfvyitUn9J2zGKPlO+2pbRaiHM=
```

### 4.5 Reference implementation (`contract/python/claim_contract/signing.py`)
```python
import base64, hashlib, hmac
from datetime import datetime, timezone

SKEW_SECONDS = 300


def canonical(method: str, target: str, ts: str, idem: str | None, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), target, ts, idem or "", body_hash]).encode()


def sign(secret: bytes, method, target, ts, idem, body) -> str:
    mac = hmac.new(secret, canonical(method, target, ts, idem, body), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def verify(secrets: dict[str, bytes], key_id, signature, method, target, ts, idem, body, now=None):
    secret = secrets.get(key_id)
    if secret is None:
        raise InvalidSignature()
    now = now or datetime.now(timezone.utc)
    sent = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if abs((now - sent).total_seconds()) > SKEW_SECONDS:
        raise StaleRequest()
    expected = sign(secret, method, target, ts, idem, body)
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature()
```

## 5. Idempotency

### 5.1 Storage
- Redis hot path: key `idem:{key_id}:{idempotency_key}` → JSON `{request_hash, status, headers, body, created_at}`, TTL 24 h.
- Durable path for claims and decisions: unique constraint `(key_id, idempotency_key)` in the owning DB table (`inbound_request` / `claim` tables) so a Redis flush cannot cause a double submit.
- `request_hash = sha256(method + target + body)`.

### 5.2 Algorithm
```
on request with idempotency key K:
  rec = redis.get(K) or db.lookup(K)
  if rec is None:
      lock = redis.set(K+":lock", nx=True, ex=60)
      if not lock: return 409 idempotency_in_progress (Retry-After: 2)
      response = execute()
      store(K, request_hash, response)  # only for 2xx and 4xx (not 5xx)
      return response
  if rec.request_hash == request_hash: return rec.response with Idempotent-Replay: true
  else: return 409 idempotency_conflict
```
5xx results are NOT stored; the client retries with the same key and the handler must itself be safe to re-run (all side effects keyed on the claim/query id).

## 6. Endpoints

All paths are relative to the receiving system's base URL (`https://insurer-api.local:8100`, `https://hospital-api.local:8000`). All bodies are JSON.

### 6.1 Hospital → Insurer (served by insurer-api)

#### 6.1.1 `POST /v1/hospital-api/claims` — submit claim
Request body = `ClaimSubmission`.
```json
{
  "contract_version": "1.0",
  "claim_ref": "HC-2026-000001",
  "claim_type": "cashless",
  "patient": {
    "full_name": "Asha Verma", "dob": "1984-03-12", "gender": "F",
    "member_id": "MEM-77120345", "policy_number": "POL-NIV-2025-004411",
    "id_proof_hash": "9f2c0e6c0a8e3b1d5f4a7c2e9b6d1a0f3c5e7b9d2f4a6c8e0b1d3f5a7c9e2b4d"
  },
  "admission": {
    "admission_type": "planned", "admitted_on": "2026-09-28", "discharged_on": "2026-10-01",
    "diagnosis_codes": ["K80.2"], "procedure_codes": ["0FT44ZZ"],
    "treating_doctor": "Dr. R. Menon", "hospital_id": "HOSP-0007", "preauth_ref": "PA-2026-33121"
  },
  "bill_lines": [
    {"code": "RM-GEN", "description": "Room rent (semi-private) x3", "category": "room",
     "qty": "3", "unit_price": {"amount": "4000.00", "currency": "INR"},
     "amount": {"amount": "12000.00", "currency": "INR"},
     "source_doc_id": "0199a1b2-1111-7000-8000-00000000000a", "source_page": 2}
  ],
  "totals": {"gross": {"amount": "12000.00", "currency": "INR"},
             "discounts": {"amount": "0.00", "currency": "INR"},
             "claimed": {"amount": "12000.00", "currency": "INR"}},
  "documents": [
    {"doc_id": "0199a1b2-1111-7000-8000-00000000000a", "doc_type": "final_bill",
     "filename": "final_bill.pdf", "sha256": "ab12...ef", "size_bytes": 184233,
     "download_url": "https://minio.local:9000/hospital-docs/...&X-Amz-Expires=86400",
     "parse_confidence": 0.93, "pages": 3}
  ],
  "config_versions": {"doc_requirements": 4, "deadlines": 2, "router_rules": 3, "confidence_gates": 1},
  "submitted_at": "2026-10-01T16:42:10Z"
}
```
Success `202 Accepted`:
```json
{
  "claim_ref": "HC-2026-000001", "insurer_claim_no": "IC-2026-000412",
  "status": "received", "received_at": "2026-10-01T16:42:11Z",
  "sequence": 1, "document_ingest": {"queued": 1, "failed": 0}
}
```
Errors: `401 invalid_signature|stale_request`, `422 validation_error|totals_mismatch`, `409 idempotency_conflict`, `409 duplicate_claim` (same `claim_ref` with different body and a different key), `424 doc_unavailable` (any URL unreachable at accept time — claim is NOT created).
Semantics: acceptance means "stored and queued for verification", not "documents downloaded". Document download is asynchronous; failure triggers `refresh-url` (6.2.5).

#### 6.1.2 `GET /v1/hospital-api/claims/{claim_ref}` — current status
`200`:
```json
{
  "claim_ref": "HC-2026-000001", "insurer_claim_no": "IC-2026-000412",
  "status": "needs_info", "status_since": "2026-10-02T09:00:00Z", "sequence": 4,
  "open_query_ids": ["0199a1b2-7777-7000-8000-000000000031"],
  "decision": null
}
```
`404 unknown_claim`. Query param `include=queries` embeds query objects.

#### 6.1.3 `POST /v1/hospital-api/claims/{claim_ref}/documents` — supplement documents
Body:
```json
{ "reason": "query_response", "query_id": "0199a1b2-7777-7000-8000-000000000031",
  "documents": [ { "doc_id": "...", "doc_type": "lab_report", "filename": "lft.pdf",
                   "sha256": "...", "size_bytes": 90211, "download_url": "https://...",
                   "parse_confidence": 0.88, "pages": 2 } ] }
```
`202` `{ "accepted": 1, "sequence": 5 }`. `409 invalid_transition` if claim is already `settled|closed`. `reason` ∈ `query_response | voluntary | correction`.

#### 6.1.4 `GET /v1/hospital-api/claims/{claim_ref}/queries`
`200` paginated list of `Query` (see 6.2.2 for shape).

#### 6.1.5 `POST /v1/hospital-api/queries/{query_id}/responses`
```json
{ "query_id": "0199a1b2-7777-7000-8000-000000000031",
  "answer_text": "LFT report attached; bilirubin trend explains extended stay.",
  "attached_doc_ids": ["0199a1b2-2222-7000-8000-0000000000b1"],
  "responded_by": "officer-17", "responded_at": "2026-10-02T14:20:00Z" }
```
`202` `{ "status": "answered", "sequence": 6 }`. `409 invalid_transition` if query not `open`/`draft_ready`. `422` if a `requested_doc_types` entry is not covered by attached docs and `answer_text` is empty.

#### 6.1.6 `POST /v1/hospital-api/claims/{claim_ref}/withdraw`
Body `{ "reason": "patient_requested", "note": "…" }` → `200` status `closed` (if not yet decided) else `409 invalid_transition`.

### 6.2 Insurer → Hospital (served by hospital-api)

#### 6.2.1 `POST /v1/insurer-callbacks/status`
```json
{ "claim_ref": "HC-2026-000001", "insurer_claim_no": "IC-2026-000412",
  "status": "verifying", "hospital_visible_status": "acknowledged",
  "sequence": 2, "occurred_at": "2026-10-01T16:45:00Z", "note": null }
```
`204 No Content`. Hospital maps `hospital_visible_status` itself using the table in 01-02 §4.3 and stores `insurer_status` raw for display.

#### 6.2.2 `POST /v1/insurer-callbacks/queries`
```json
{ "claim_ref": "HC-2026-000001", "sequence": 4,
  "query": {
    "query_id": "0199a1b2-7777-7000-8000-000000000031", "round": 1,
    "category": "missing_document",
    "text": "Please provide the histopathology report for the removed gallbladder.",
    "requested_doc_types": ["investigation_report"],
    "due_by": "2026-10-09T17:00:00Z", "status": "open",
    "raised_by": "agent:query-drafter+human:reviewer-4" } }
```
`204`. Idempotent on `query_id`; a re-sent query with changed text → `409 idempotency_conflict`.

#### 6.2.3 `POST /v1/insurer-callbacks/decisions`
```json
{ "claim_ref": "HC-2026-000001", "sequence": 9,
  "decision": {
    "outcome": "partial", "approved_amount": {"amount": "41500.00", "currency": "INR"},
    "deductions": [ {"line_ref": "RM-GEN", "rule_id": "room_rent_cap_1pct", "amount": {"amount": "3000.00", "currency": "INR"},
                     "explanation": "Room rent capped at 1% of sum insured per day."} ],
    "reason_codes": ["ROOM_CAP"], "reviewer_ids": ["rev-4", "appr-2"],
    "calc_trace_id": "0199a1b2-9999-7000-8000-000000000071",
    "policy_version": 6, "decided_at": "2026-10-04T11:05:00Z" } }
```
`204`. Decision is final per round; a later correction arrives as a new decision with higher `sequence` and `supersedes` field (v1.1 reserved).

#### 6.2.4 `POST /v1/insurer-callbacks/settlements`
```json
{ "claim_ref": "HC-2026-000001", "sequence": 10,
  "settlement": { "settlement_id": "0199a1b2-aaaa-7000-8000-000000000081",
                  "amount": {"amount": "41500.00", "currency": "INR"},
                  "utr": "SIMUTR20261005000123", "paid_on": "2026-10-05",
                  "mode": "NEFT", "tds": {"amount": "0.00", "currency": "INR"} } }
```
`204`.

#### 6.2.5 `POST /v1/insurer-callbacks/documents/{doc_id}/refresh-url` (**v1.1**, PROPOSED)
Used when a presigned URL expired before the insurer downloaded the file.
Request: `{ "claim_ref": "HC-2026-000001", "reason": "url_expired" }`
`200`:
```json
{ "doc_id": "0199a1b2-1111-7000-8000-00000000000a",
  "download_url": "https://minio.local:9000/hospital-docs/...&X-Amz-Expires=3600",
  "expires_at": "2026-10-06T11:15:30Z", "sha256": "ab12...ef", "size_bytes": 184233 }
```
Errors: `404 unknown_document`, `403 not_your_claim` (doc doesn't belong to a claim of this insurer), `429 rate_limited` (limit 10 refreshes per doc per hour). The hospital re-presigns for ≤ 1 h.

### 6.3 Utility endpoints (both systems, unsigned but network-restricted)
- `GET /v1/health` → `200 {"status":"ok","version":"1.1.0","time":"..."}`; `503` if a dependency is down.
- `GET /v1/contract` → `200 {"supported":["1.0","1.1"],"default":"1.1","deprecated":[]}`.

## 7. Documents transfer (flow)
1. Hospital stores file in MinIO `hospital-docs`, computes `sha256`.
2. On submit, hospital presigns GET URLs (TTL 24 h) per document.
3. Insurer worker downloads, checks `size_bytes` and `sha256`; mismatch → mark `doc_corrupt`, raise a `needs_info` query internally (`illegible_document`).
4. Copies into `insurer-docs`, runs its own ClamAV scan, then parsing.
5. On `403/404/expired` → call `refresh-url`; after 3 failed refreshes → `needs_info` query `missing_document`.
Hospital never receives insurer bucket credentials; the insurer never writes to hospital storage.

## 8. Error model (RFC 7807 + extensions)
```json
{ "type": "https://claims.local/errors/totals_mismatch", "title": "Totals mismatch", "status": 422,
  "detail": "Sum of bill_lines (12500.00) differs from totals.gross (12000.00).",
  "code": "totals_mismatch",
  "errors": [ {"field": "totals.gross", "message": "expected 12500.00"} ],
  "trace_id": "0199a1b2-bbbb-7000-8000-000000000099", "retryable": false }
```
### 8.1 Error catalogue
| code | HTTP | Retryable | When | Client action |
|---|---|---|---|---|
| `unsupported_version` | 400 | no | major not supported | upgrade/downgrade |
| `hospital_blacklisted` | 403 | no | hospital not permitted by insurer (PROPOSED) | contact insurer |
| `hospital_mismatch` | 403 | no | `admission.hospital_id` differs from hospital bound to `X-Key-Id` (PROPOSED) | fix hospital id |
| `payload_too_large` | 413 | no | body over 2 MB (PROPOSED) | send documents by URL |
| `bad_request` | 400 | no | malformed JSON | fix client |
| `invalid_signature` | 401 | no | bad/missing signature or unknown key | check secret |
| `stale_request` | 401 | yes (re-sign) | timestamp skew | re-sign with fresh ts |
| `forbidden` / `not_your_claim` | 403 | no | key not authorised for resource | – |
| `unknown_claim` / `unknown_query` / `unknown_document` | 404 | no | id not found | – |
| `duplicate_claim` | 409 | no | same `claim_ref`, different payload, new key | use status endpoint |
| `idempotency_conflict` | 409 | no | same key, different payload | generate new key |
| `idempotency_in_progress` | 409 | yes | first call still running | retry after `Retry-After` |
| `invalid_transition` | 409 | no | state machine forbids | refresh status |
| `payload_too_large` | 413 | no | body > 2 MB | send docs by URL |
| `validation_error` | 422 | no | schema/field errors | fix payload |
| `totals_mismatch` | 422 | no | lines vs totals | fix totals |
| `rate_limited` | 429 | yes | >100 req/min | back off |
| `internal_error` | 500 | yes | unexpected | retry same key |
| `doc_unavailable` | 424 | yes | doc URL unreachable | refresh then retry |
| `service_unavailable` | 503 | yes | dependency down | retry |

## 9. Delivery guarantees, retries, outbox
### 9.1 Semantics
At-least-once. Receivers are idempotent; senders retry until a 2xx or 4xx (except `stale_request`, `idempotency_in_progress`, `rate_limited`, which are retried).

### 9.2 Ordering
Every claim has a monotonically increasing `sequence` per direction counter. Receiver rule: if `incoming.sequence <= last_applied_sequence` → ignore body, return the stored success (`204`/`Idempotent-Replay`). If `incoming.sequence > last_applied + 1` → accept but flag `gap_detected` and call `GET status` to reconcile (do not block).

### 9.3 Outbox table (each side)
```sql
CREATE TABLE outbox_message (
  id UUID PRIMARY KEY,
  claim_ref TEXT NOT NULL,
  endpoint TEXT NOT NULL,            -- path
  method TEXT NOT NULL DEFAULT 'POST',
  body JSONB NOT NULL,
  idempotency_key UUID NOT NULL,
  sequence BIGINT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|sending|delivered|dead
  attempts INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at TIMESTAMPTZ
);
CREATE INDEX outbox_due ON outbox_message (status, next_attempt_at);
```
### 9.4 Sender pseudocode (`OutboxSender`)
```python
BACKOFF = [1, 4, 16, 60, 300, 900, 1800, 3600]  # seconds, max 8 attempts


async def run_once(db, http, signer):
    rows = await db.fetch_due(limit=20, for_update_skip_locked=True)
    for m in rows:
        ts = utcnow_iso()
        sig = signer.sign(m.method, m.endpoint, ts, str(m.idempotency_key), canonical_bytes(m.body))
        try:
            r = await http.request(
                m.method,
                base_url + m.endpoint,
                content=canonical_bytes(m.body),
                headers=headers(ts, sig, m),
            )
        except (Timeout, ConnectError) as e:
            await reschedule(m, str(e))
            continue
        if 200 <= r.status_code < 300:
            await mark_delivered(m)
        elif r.status_code in (401,) and code(r) == "stale_request":
            await reschedule(m, "stale", immediate=True)
        elif (
            r.status_code in (429, 503)
            or r.status_code >= 500
            or code(r) == "idempotency_in_progress"
        ):
            await reschedule(m, f"{r.status_code}", retry_after=r.headers.get("Retry-After"))
        else:  # 4xx permanent
            await mark_dead(m, f"{r.status_code} {code(r)}")


async def reschedule(m, err, immediate=False, retry_after=None):
    m.attempts += 1
    if m.attempts >= 8:
        return await mark_dead(m, err)
    delay = float(retry_after) if retry_after else BACKOFF[m.attempts - 1]
    m.next_attempt_at = now() + timedelta(seconds=delay * jitter(0.8, 1.2))
```
Dead messages surface in the owning UI "Failed deliveries" panel with a manual **Retry** (resets attempts, same idempotency key) and fire an alert (see 05-integration doc 04).

### 9.5 Receiver inbox (dedupe)
```sql
CREATE TABLE inbound_request (
  key_id TEXT NOT NULL, idempotency_key UUID NOT NULL,
  request_hash CHAR(64) NOT NULL, status_code INT NOT NULL, response JSONB,
  claim_ref TEXT, sequence BIGINT, received_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (key_id, idempotency_key)
);
```

## 10. Rate limits and sizes
- 100 requests / minute per `key_id` (token bucket, Redis). `refresh-url`: 10/doc/hour.
- Max body 2 MB (documents travel by URL). Max `bill_lines` 2,000; max `documents` 100; `answer_text` ≤ 8,000 chars.
- Timeouts: client connect 3 s, read 15 s; server handler budget 10 s (claims POST must not parse documents inline).

## 11. Versioning policy
- Minor (additive optional fields, new endpoints) → same `/v1` path; receivers MUST ignore unknown response fields; requests use `extra="forbid"` only for known version (unknown fields in a request of a *higher* minor than supported → `422` listing fields).
- Major → new `/v2` path served in parallel for one phase; `GET /v1/contract` lists `deprecated`.
- Store `contract_version` with every claim and message on both sides.
- Change process: PR touching `contract/` + this doc; both approve; bump `contract_version`, regenerate OpenAPI, update tpa-sim and hospital-sim.

## 12. Contract v1.1 additions (PROPOSED)
### 12.1 `X-Journey-Id`
- Purpose: one id that follows a claim across both systems for tracing (Langfuse/log correlation) without sharing internal ids.
- Format: UUIDv7 generated by the hospital when the case is created; stored on `claim_case.journey_id`; sent on every hospital→insurer request; the insurer stores it on `case.journey_id` and echoes it on every callback.
- Not part of the signature canonical string (so v1.0 verifiers remain compatible) but logged with every request. Missing header is allowed (generated by the first receiver).
- Privacy: random id; never derived from patient data.
### 12.2 Document refresh endpoint — see 6.2.5.
### 12.3 Reserved (documented, not yet implemented)
`Decision.supersedes` (sequence number of the decision replaced), `StatusUpdate.eta` (ISO date). Receivers ignore unknown fields.
### 12.4 Negotiation
Senders send the highest minor they know. `GET /v1/contract` tells them the peer's `supported` list; if the peer lacks 1.1 they omit v1.1-only headers/endpoints.

## 13. Build tasks
1. `contract/openapi/claims-v1.yaml` — author from sections 3, 6, 8 (components: models from 01-02, headers, error responses). Validate with `openapi-spec-validator`.
2. `contract/python/claim_contract/signing.py` — `canonical`, `sign`, `verify`; add `tests/test_signing.py` using vectors V1 and V2 verbatim, plus the failure cases in section 14.
3. `contract/python/claim_contract/idempotency.py` — Redis+DB store, FastAPI dependency `IdempotencyGuard`, `request_hash` helper, `Idempotent-Replay` header.
4. `contract/python/claim_contract/outbox.py` — `OutboxSender` per 9.4, SQLAlchemy model from 9.3, metrics hooks (`outbox_pending`, `outbox_dead`).
5. `contract/python/claim_contract/inbox.py` — sequence check helper (`apply_sequence`) per 9.2.
6. `contract/python/claim_contract/errors.py` — `ProblemDetail`, exception classes, FastAPI exception handlers producing section 8 JSON.
7. `contract/python/claim_contract/middleware.py` — `HMACAuthMiddleware` (rules 4.2), rate limiter, request-id and journey-id propagation.
8. `contract/tests/contract/` — schemathesis config running against any base URL; fixtures in `contract/tests/fixtures/`.
9. `services/tpa-sim` — implements section 6.1 and sends 6.2 callbacks (Dev B). `hospital/api/tests/hospital_sim.py` — implements 6.2 receiver and a hospital-side sender (Dev A).
10. `contract/CHANGELOG.md` — record 1.0 and 1.1.

## 14. Test matrix
| # | Case | Expected |
|---|---|---|
| T1 | V1 and V2 vectors | exact signature match |
| T2 | tampered body byte | 401 invalid_signature |
| T3 | tampered path/query | 401 invalid_signature |
| T4 | timestamp +301 s / −301 s | 401 stale_request |
| T5 | timestamp ±299 s | accepted |
| T6 | unknown key id | 401 invalid_signature |
| T7 | same idempotency key + body twice | second has `Idempotent-Replay`, one DB row |
| T8 | same key, different body | 409 idempotency_conflict |
| T9 | concurrent same key (2 requests) | one executes, one 409 in_progress or replay |
| T10 | Redis flushed between retries | DB constraint prevents duplicate claim |
| T11 | sequence out of order (5 then 4) | 4 ignored, success returned |
| T12 | sequence gap (3 → 6) | applied, `gap_detected` logged, status reconcile |
| T13 | 500 then 200 with same key | exactly one side effect |
| T14 | expired doc URL | refresh-url → new URL, ingest succeeds |
| T15 | refresh 11th time in an hour | 429 |
| T16 | duplicate query callback | 204, one query row |
| T17 | major version `2.0` header | 400 unsupported_version |
| T18 | body > 2 MB | 413 |
| T19 | totals mismatch | 422 totals_mismatch with field |
| T20 | outbox 8 failures | status dead, alert metric incremented |
| T21 | withdraw after decision | 409 invalid_transition |
| T22 | schemathesis full run, both servers | no 5xx, all responses match schema |
| T23 | v1.1 sender → v1.0 receiver | `X-Journey-Id` ignored, works |

## 15. Acceptance criteria
- Both servers pass T1-T23; signing vectors reproduced by Python test and a second language (curl + openssl script in `contract/tests/vectors.sh`).
- Kill-and-retry test (T13) shows exactly one effect.
- OpenAPI validates; generated docs list every endpoint above.
- Contract frozen tag `contract-v1.0` created at end of Phase 0.

## 16. Dependencies
`01-02` (models), `01-04` (audit events `claim.submitted`, `ack.received`), `01-05` (repo), Redis (04-shared-services/01).

## 17. Claude Code kickoff prompt
> Read docs/implementation/01-shared-contract/01-api-contract-v1.md and 02-data-models-and-enums.md. Implement build tasks 1-8 inside `contract/` only. Use signing vectors V1/V2 verbatim in tests. Do not write business logic for either server. Stop when T1-T20 pass and report which of T21-T23 need the servers.
