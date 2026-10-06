# 02-03 — hospital-api: Cases and Documents (upload, scan, parse trigger)

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/{routers/cases.py,routers/documents.py,routers/internal_documents.py,services/cases.py,services/documents.py,services/transitions.py,storage/}`.

## 1. Goal
Create and manage claim cases and take in documents safely: validate, virus-scan, store in MinIO, de-duplicate, kick off parsing (doc-pipeline, vision-service) and classification, and expose results to the desk. This is the entry point of the hospital pipeline: everything downstream (completeness, router, claim builder) reads the rows produced here.

Out of scope: completeness rules (doc 04), router (doc 05), claim drafts and submission (doc 06), query attachments (doc 07; they reuse `ingest()` from this doc), the parsing internals themselves (`04-shared-services/02-doc-pipeline.md`, `03-vision-service.md`).

## 2. Inputs / Outputs
- In: desk uploads (PDF, JPG, PNG, TIFF, ≤ 25 MB each, ≤ 60 files/case, ≤ 200 pages/PDF), patient and policy data, manual reclassification, internal callbacks from n8n/crew.
- Out:
  - `claim_case`, `case_status_history`, `patient`, `insurance_policy_ref`, `document`, `document_parse` rows (schema: doc 01).
  - MinIO objects in bucket `hospital-docs`.
  - n8n webhook `intake/document-uploaded` (idempotent) per accepted document.
  - SSE events (`document.uploaded`, `document.scanned`, `document.parsed`, `document.classified`, `case.status_changed`) via the hub in doc 07.
  - Audit events (`case.created`, `doc.uploaded`, `doc.scanned`, `doc.parsed`, `doc.classified`, `doc.deleted`, `doc.reclassified`), none containing PII.

## 3. Data model
Tables from doc 01: `claim_case`, `case_status_history`, `allowed_transition`, `patient`, `insurance_policy_ref`, `document`, `document_parse`, `doc_request`.

MinIO bucket `hospital-docs` (SSE-S3 on). Key layout:
```
{case_id}/{doc_id}/original.{ext}          the upload (immutable)
{case_id}/{doc_id}/pages/{n}.png           page renders (vision-service / UI preview)
{case_id}/{doc_id}/parsed.md               raw parsed markdown (may contain PII; never leaves the host)
{case_id}/{doc_id}/masked.txt              Presidio-masked text (only this may go to external LLMs)
quarantine/{case_id}/{doc_id}.bin          infected uploads, no download route
tombstone/{case_id}/{doc_id}/original.{ext}  deleted docs retained 30 days
```
Lifecycle rule: `tombstone/` and `quarantine/` expire after 30 days.

Document lifecycle (column `document.lifecycle`):
```
active ──supersede──► superseded
active ──delete────► deleted (tombstone, purge_after = now+30d)
(scan infected) ───► quarantined (terminal)
```
Document processing states (`parse_status`): `pending → processing → parsed | needs_review | failed`; `reparse` returns to `pending`.

Derived fields exposed by the API: `usable` (= lifecycle active ∧ scan clean ∧ not `illegible_candidate`), `needs_attention` (= parse_status needs_review/failed or doc_type null).

## 4. API / endpoints

Auth per doc 02. Desk is row-scoped via `case_scope()`. All responses are JSON; errors follow RFC 7807 (01-01 §7).

### 4.1 Cases
```
POST   /v1/cases                         desk|officer
GET    /v1/cases?status=&q=&assigned=&claim_type=&flag=&cursor=&size=     list (scoped, keyset)
GET    /v1/cases/{id}                    detail incl. checklist summary, route, flags, last completeness
PATCH  /v1/cases/{id}                    editable fields (If-Match: <version>)
POST   /v1/cases/{id}/assign             officer|admin: {user_id}
POST   /v1/cases/{id}/transition         guarded manual transitions {to, reason}
GET    /v1/cases/{id}/timeline           status history + audit summary
```

**`POST /v1/cases`** request:
```json
{"patient":{"uhid":"UH-20458","full_name":"Ravi Kumar","dob":"1984-03-12","gender":"M","phone":"+919800000000"},
 "policy":{"insurer_name":"Acme Health","policy_number":"AH-993201","member_id":"M-77123"},
 "claim_type":"cashless","admission_type":"planned",
 "admitted_on":"2026-09-28","discharged_on":"2026-10-02","preauth_ref":"PA-5521","treating_doctor":"Dr. Rao"}
```
Response **201** (`Location: /v1/cases/{id}`):
```json
{"id":"0190d3c1-...","claim_ref":"HC-2026-000123","status":"draft","version":1,
 "filing_deadline":null,"warnings":[{"code":"preauth_not_found","field":"preauth_ref"}]}
```
Validation rules:
| Field | Rule | Failure |
|---|---|---|
| `patient.dob` | not in future, age ≤ 120 | 422 `validation_error` |
| `patient.gender` | `M|F|O` | 422 |
| `discharged_on` | ≥ `admitted_on`, not in future (draft allows null) | 422 |
| `claim_type=cashless` | `preauth_ref` expected → missing is a **warning** (pre-auth is simulated, PROPOSED) | 201 with warning |
| `claim_type=reimbursement` | computes `filing_deadline = discharged_on + deadlines.reimbursement_filing_days` (config) | – |
| `policy.*` | non-empty, trimmed, ≤ 64 chars | 422 |
| `patient.uhid` exists | patient upserted by UHID; if name/DOB differ → 409 `patient_conflict` with the stored values (desk must confirm via `?confirm_patient_update=true`) | 409 |

**`GET /v1/cases`** → 200
```json
{"items":[{"id":"...","claim_ref":"HC-2026-000123","patient_name":"Ravi Kumar","claim_type":"cashless","status":"docs_pending",
           "flags":["implant"],"assigned_to":{"id":"...","name":"Desk One"},"claimed_amount":null,
           "doc_counts":{"total":4,"needs_attention":1},"updated_at":"2026-10-06T09:12:00Z"}],
 "next_cursor":"eyJ0IjoiMjAyNi0xMC0wNlQwOToxMjowMFoiLCJpIjoiMDE5MC4uLiJ9","total_estimate":214}
```
Filters: `status` (repeatable), `q` (trigram over patient name, claim_ref, UHID, ≥ 3 chars), `assigned` (`me`|`unassigned`|uuid), `claim_type`, `flag`. Sort fixed: `created_at desc, id desc` (keyset cursor = base64 of both). `size` ≤ 100 (default 25).

**`GET /v1/cases/{id}`** → 200 (excerpt)
```json
{"id":"...","claim_ref":"HC-2026-000123","status":"docs_pending","version":4,
 "patient":{"id":"...","uhid":"UH-20458","full_name":"Ravi Kumar","dob":"1984-03-12","gender":"M","phone_last4":"0000"},
 "policy":{"insurer_name":"Acme Health","policy_number":"AH-993201","member_id":"M-77123"},
 "claim_type":"cashless","admission_type":"planned","route":{"steps":["preauth_check","completeness","claim_build","officer_signoff","submit"]},
 "flags":["implant"],"admitted_on":"2026-09-28","discharged_on":"2026-10-02","preauth_ref":"PA-5521",
 "completeness":{"run_no":3,"complete":false,"blockers":2,"warnings":1},
 "config_versions":{"doc_requirements":3,"deadlines":1,"router_rules":2,"confidence_gates":1},
 "allowed_transitions":["docs_complete"]}
```
`allowed_transitions` is computed from `allowed_transition` filtered by the caller's role so the UI never guesses.

**`PATCH /v1/cases/{id}`** — header `If-Match: 4` required. Editable while status ∈ `draft, docs_pending, docs_complete`: `admitted_on, discharged_on, diagnosis_codes, procedure_codes, treating_doctor, preauth_ref, claimed_amount (draft only)`, patient demographic fields, policy fields. After `building_claim` only `treating_doctor` is editable (the rest must go through claim draft edits, doc 06). Responses: 200 new version; 412 `precondition_failed` (missing/ stale `If-Match`) ; 409 `invalid_state`.

**`POST /v1/cases/{id}/assign`** `{"user_id":"..."}` → 200. Target must be active desk/officer; audit `case.assigned`. `{"user_id":null}` unassigns.

**`POST /v1/cases/{id}/transition`** `{"to":"docs_complete","reason":"officer override"}` → 200 or 409 `invalid_transition` (body lists allowed). Manual transitions are limited to a whitelist (PROPOSED): `docs_pending↔docs_complete` (officer, requires reason), `draft→closed` (cancel, officer), `docs_complete→docs_pending` (desk). System transitions (`building_claim`, `submitted`, insurer-driven states) are never exposed here.

**`GET /v1/cases/{id}/timeline`** → 200 `{"events":[{"ts":"...","kind":"status","from":"draft","to":"docs_pending","actor":"Desk One"},{"ts":"...","kind":"audit","type":"doc.uploaded","summary":"bill.pdf uploaded"}]}` paginated (cursor).

### 4.2 Documents
```
POST   /v1/cases/{id}/documents          multipart (files[]), optional doc_type_hint[] and supersedes_id per file
GET    /v1/cases/{id}/documents          ?lifecycle=active|all &doc_type= &needs_attention=
GET    /v1/documents/{doc_id}            metadata + parse summary
GET    /v1/documents/{doc_id}/download   302 to presigned URL (5 min)
GET    /v1/documents/{doc_id}/pages/{n}  302 to presigned page preview PNG
PATCH  /v1/documents/{doc_id}            {doc_type} manual reclassify (officer/desk)
DELETE /v1/documents/{doc_id}            only before submission; creates tombstone, object retained 30 d
POST   /v1/documents/{doc_id}/reparse    re-run pipeline
GET    /v1/documents/{doc_id}/parse      typed JSON + per-field confidence + both passes
```

**`POST /v1/cases/{id}/documents`** — `multipart/form-data`, field `files` (repeatable), optional `doc_type_hint` (same order), optional `supersedes_id`.
Response **202**:
```json
{"documents":[
  {"id":"0190d3c9-...","filename":"bill.pdf","status":"accepted","scan_status":"clean","parse_status":"pending"},
  {"filename":"dup.pdf","status":"skipped_duplicate","duplicate_of":"0190d3c9-..."},
  {"filename":"scan.exe","status":"rejected","error":{"code":"unsupported_media_type","detail":"application/x-dosexec"}},
  {"filename":"eicar.pdf","status":"rejected","error":{"code":"infected","signature":"Eicar-Test-Signature"}}]}
```
The request is processed per file; HTTP status is 202 if at least one file accepted or skipped, 422 if **all** files were rejected (body is the same structure), 413 if the whole request exceeds `HOSP_MAX_UPLOAD_MB × files` or the file count > remaining quota, 423 if the case is locked (§8), 503 `scan_unavailable` if ClamAV is down (fail closed).

**`GET /v1/documents/{doc_id}`** → 200
```json
{"id":"...","case_id":"...","filename":"bill.pdf","mime_type":"application/pdf","size_bytes":482113,"pages":3,
 "sha256":"9f2c...","scan_status":"clean","lifecycle":"active",
 "doc_type":"itemised_bill","doc_type_source":"auto","classification_confidence":0.93,
 "parse_status":"parsed","parse_confidence":0.88,"agreement_score":0.96,
 "quality":{"score":0.82,"flags":[],"has_required_stamp":true},
 "supersedes_id":null,"parent_id":null,"uploaded_by":{"id":"...","name":"Desk One"},"created_at":"2026-10-06T09:05:11Z",
 "usable":true,"needs_attention":false}
```

**`GET /v1/documents/{doc_id}/parse`** → 200 `{"passes":[{"pass_no":1,"engine":"mineru-pipeline","confidence":0.88,"typed_json":{...}},{"pass_no":2,"engine":"crew-llm","confidence":0.9,"typed_json":{...}}],"agreement_score":0.96,"fields_disagreeing":["total"]}`. Raw markdown is not returned; only `typed_json` and masked excerpts.

**`PATCH /v1/documents/{doc_id}`** `{"doc_type":"lab_report"}` → 200; sets `doc_type_source='manual'` (wins forever); triggers a completeness re-run (doc 04) and audit `doc.reclassified` (old, new, user). 409 if document lifecycle ≠ active or case submitted.

**`DELETE /v1/documents/{doc_id}`** → 204. Allowed in statuses `draft, docs_pending, docs_complete, ready_for_review` (the last returns the case to `docs_pending`, and invalidates the latest draft sign-off). 409 `case_submitted` after submission. Moves the object to `tombstone/`, sets `lifecycle='deleted'`, `deleted_by`, `purge_after = now()+30d`.

**`POST /v1/documents/{doc_id}/reparse`** → 202; resets `parse_status='pending'`, `parse_attempts=0`, deletes pass 2 only (pass 1 kept for audit unless `?full=true`), re-triggers n8n. 409 if parse is `processing`.

### 4.3 Internal callbacks (service auth `svc-n8n` / `svc-crew`)
```
POST /v1/internal/documents/{id}/quality     {quality_score, flags[], has_required_stamp}
POST /v1/internal/documents/{id}/parse       {pass_no, engine, engine_version, typed_json, confidence, masked_text_key, entities?, duration_ms}
POST /v1/internal/documents/{id}/classify    {doc_type, confidence, source:"auto"}
POST /v1/internal/documents/{id}/status      {parse_status, error?}
POST /v1/internal/documents/{id}/split       {children:[{doc_type_hint, page_from, page_to}]}   (mixed PDF)
GET  /v1/internal/documents/pending-parse    ?older_than_s=120&limit=50   (sweeper source)
```
Each callback validates with Pydantic (`extra="forbid"`), is idempotent on `(document_id, pass_no)` or on identical payload hash, persists, appends an audit event and publishes SSE. Wrong-service tokens get 403 `wrong_service`. Examples:

`POST /v1/internal/documents/{id}/parse`
```json
{"pass_no":1,"engine":"mineru-pipeline","engine_version":"2.1.0","confidence":0.88,
 "typed_json":{"doc_kind":"itemised_bill","lines":[{"description":"Room rent","qty":4,"amount":"16000.00"}],"total":"148230.00"},
 "masked_text_key":"0190.../masked.txt","duration_ms":8120}
```
→ 200 `{"document_id":"...","parse_status":"processing","next":"pass_2_expected"}`; when pass 2 arrives the response includes `agreement_score` and the resulting `parse_status`.

## 5. Build tasks
1. `storage/minio.py`: async wrapper (aioboto3), `put_stream`, `copy`, `presign_get(ttl)`, `delete`, `exists`; bucket bootstrap with SSE-S3 and lifecycle rules (tombstone and quarantine 30 d); health probe.
2. `services/cases.py`: `create_case()` — one transaction: upsert patient by UHID (conflict rule §4.1), upsert policy ref, `next_claim_ref()`, insert case with `config_versions` snapshot (resolve each domain via `ConfigService`), status history row, audit `case.created`, compute `filing_deadline`, preauth warning lookup (doc 05 `simulated_preauth`).
3. `routers/cases.py` with scope filter (doc 02 task 11), keyset pagination (`(created_at,id)` cursor), trigram search, `allowed_transitions` computation.
4. Optimistic locking helper `with_version(case, if_match)` mapping header → `WHERE version = :v`; 412 when header missing on PATCH, 409/412 on mismatch.
5. `services/transitions.py::transition(case, to, actor, reason)`: contract `assert_transition`, whitelist for manual route, write history + audit, publish SSE `case.status_changed`; DB trigger is the safety net.
6. Upload pipeline `services/documents.py::ingest(case, upload, user, hint, supersedes_id)`:
   1. stream to temp (spooled, max 25 MB; reject early via `Content-Length` and while streaming);
   2. sniff magic bytes (`python-magic`), allowed MIME whitelist (`application/pdf`, `image/jpeg`, `image/png`, `image/tiff`); reject mismatch with extension;
   3. compute sha256 while streaming;
   4. duplicate check `(case_id, sha256)` → return existing as `skipped_duplicate`;
   5. send to ClamAV (`clamd` INSTREAM over TCP 3310); infected → insert row `scan_status=infected, lifecycle=quarantined`, store object under `quarantine/`, audit `doc.scanned` (infected), file rejected with `infected`;
   6. PDF sanity: open with `pikepdf`; reject encrypted-without-password, flag embedded files/JavaScript (reject), page count ≤ 200;
   7. images: strip EXIF GPS (keep orientation then normalise), reject decompression bombs (`Image.MAX_IMAGE_PIXELS = 100M`);
   8. put to MinIO, insert `document` row, audit `doc.uploaded`, `doc.scanned`, publish SSE;
   9. enqueue processing: POST n8n webhook `intake/document-uploaded` `{case_id, document_id}` (idempotency key = document_id) and set `last_trigger_at`.
7. Internal callback endpoints (§4.3) with Pydantic schemas, service-role guard, audit and SSE.
8. Classification gate (deterministic): accept auto `doc_type` only if confidence ≥ `confidence_gates.classification_min` (PROPOSED 0.80) else `parse_status='needs_review'` and `doc_type=NULL`; manual choice sets `doc_type_source='manual'` and wins forever (auto results never overwrite).
9. Two-pass agreement: when pass 2 arrives compute `agreement_score` over key fields (amounts, dates, names via normalised equality; money compared as Decimal; names via token-set ratio ≥ `HOSP_NAME_MATCH_MIN`); below `confidence_gates.agreement_min` (PROPOSED 0.90) → `needs_review`; at or above and parse confidence ≥ `parse_min` → `parsed`.
10. Supersede flow: uploading with `supersedes_id` (same case, active) marks the old doc `lifecycle='superseded'` in the same transaction; superseded docs are excluded from completeness.
11. Delete rules and tombstoning (§4.2); on delete re-run completeness; if the case was `ready_for_review` invalidate sign-off drafts.
12. Auto status transitions: first accepted upload moves `draft → docs_pending`; completeness results (doc 04) drive `docs_complete`; deleting the only doc does not revert to `draft`.
13. Rate limit uploads (Redis token bucket, 30 files/min/user) → 429 `rate_limited` with `Retry-After`.
14. Pydantic schemas in `schemas/cases.py`, `schemas/documents.py`; OpenAPI examples copied from §4.
15. Cleanup jobs (`app/jobs/`, run by n8n cron via internal endpoint or APScheduler fallback):
    - stale drafts > 30 d → set `stale_flagged_at`, notify admin (no auto delete);
    - tombstone purge (`purge_after < now()`): delete object and null `storage_key` (row kept for audit);
    - orphan scan: MinIO keys with no `document` row (daily), report only;
    - sweeper: documents `parse_status='pending'` ∧ `scan_status='clean'` ∧ `last_trigger_at < now()-2min` → re-trigger n8n (max 5 attempts via `parse_attempts`, then `failed` + SSE + audit).
16. Mixed-document splitting: `POST /internal/documents/{id}/split` creates child `document` rows (`parent_id`, `page_range`, derived object keys using page extraction via `pikepdf`), marks the parent `lifecycle='superseded'`, triggers parsing per child.
17. Download/preview: presigned URL TTL `HOSP_PRESIGN_TTL_S` (300), `Content-Disposition: attachment`, audit `doc.downloaded`; quarantined/deleted docs return 404/410.
18. Tests and fixtures per §9; seed fixtures in `data/synthetic/docs/` (clean PDF, EICAR, encrypted PDF, oversize, mixed PDF).

## 6. Key logic (pseudocode)
```python
async def ingest(case, upload, user, hint=None, supersedes_id=None):
    ensure_uploadable(case)                                          # status/lock rules §8
    meta = await stream_to_spool(upload, max_bytes=25*MB)            # computes sha256, size, head bytes
    if (ex := await docs.by_hash(case.id, meta.sha256)): return Skipped(ex.id)
    mime = sniff(meta.head); ensure(mime in ALLOWED and ext_matches(mime, upload.filename))
    scan = await clam.scan(meta.spool)                               # raises ScanUnavailable (fail closed)
    if scan.infected: await quarantine(case, meta, scan.sig, user); return Rejected("infected", sig=scan.sig)
    ensure_pdf_ok(meta) if mime == "application/pdf" else normalise_image(meta)
    doc_id = uuid7(); key = f"{case.id}/{doc_id}/original.{ext_for(mime)}"
    try:
        async with db.begin():
            await minio.put(key, meta.spool)
            doc = await docs.insert(id=doc_id, case_id=case.id, storage_key=key, sha256=meta.sha256, ...)
            if supersedes_id: await docs.supersede(supersedes_id, by=doc_id, case_id=case.id)
            await audit.append(case.id, "doc.uploaded", {"doc_id": str(doc_id), "mime": mime, "size": meta.size}, actor=user)
            if case.status == "draft": await transitions.system(case, "docs_pending", actor=user)
    except UniqueViolation:                                          # race with identical upload
        await minio.delete_quietly(key); return Skipped((await docs.by_hash(case.id, meta.sha256)).id)
    await n8n.trigger("intake/document-uploaded", {"case_id": case.id, "document_id": doc.id}, idem=str(doc.id))
    return Accepted(doc)
```
If the n8n trigger fails, the document stays `parse_status=pending`; the sweeper re-triggers.

Pass-2 agreement:
```python
def agreement(p1: dict, p2: dict, keys) -> float:
    scores = []
    for k in keys:  # keys come from DOC_KEY_FIELDS[doc_type]
        a, b = norm(p1.get(k)), norm(p2.get(k))
        scores.append(1.0 if a == b else name_ratio(a, b) if is_name(k) else 0.0)
    return sum(scores) / len(scores) if scores else 0.0
```
Status derivation after each callback:
```
if scan != clean or lifecycle != active: stay
if pass1 only: parse_status = processing
if pass2 present:
    if doc_type is NULL (classification gate failed) or agreement < agreement_min or parse_conf < parse_min: needs_review
    else parsed
```

### 6.1 Case creation pseudocode
```python
async def create_case(req, user):
    async with db.begin():
        patient = await patients.upsert_by_uhid(req.patient, confirm=req.confirm_patient_update)   # may raise PatientConflict
        policy = await policies.upsert(patient.id, req.policy)
        cfg = await config.snapshot(["doc_requirements","deadlines","router_rules","confidence_gates"], at=now())
        ref = await db.scalar(select(func.next_claim_ref()))
        case = await cases.insert(claim_ref=ref, patient_id=patient.id, policy_ref_id=policy.id, ..., config_versions=cfg,
                                  filing_deadline=compute_filing_deadline(req, cfg), created_by=user.id)
        await history.add(case.id, None, "draft", actor=user.id)
        await audit.append(case.id, "case.created", {"claim_type": req.claim_type, "admission_type": req.admission_type},
                           config_versions=cfg, actor=user)
    warnings = await preauth.check(req.preauth_ref, policy.member_id)                              # doc 05, never blocks
    return CaseCreated(case, warnings)
```

### 6.2 Keyset pagination
```sql
SELECT ... FROM claim_case c JOIN patient p ON p.id = c.patient_id
WHERE (:scope_filter)
  AND (:status IS NULL OR c.status = ANY(:status))
  AND (c.created_at, c.id) < (:cursor_ts, :cursor_id)
ORDER BY c.created_at DESC, c.id DESC LIMIT :size + 1;
```
Fetch `size + 1` rows; if an extra row exists emit `next_cursor` from the last returned row. Search (`q`) adds `AND (p.full_name % :q OR c.claim_ref ILIKE :q || '%' OR p.uhid ILIKE :q || '%')` with `SET LOCAL pg_trgm.similarity_threshold = 0.3`.

### 6.3 Allowed-transition computation
```python
def allowed_for(case, user) -> list[str]:
    db_allowed = TRANSITIONS[case.status]  # from contract table
    manual = MANUAL_WHITELIST.get((case.status, role_of(user)), set())
    return sorted(db_allowed & manual)


MANUAL_WHITELIST = {
    ("docs_pending", "officer"): {"docs_complete", "closed"},
    ("docs_complete", "officer"): {"docs_pending", "closed"},
    ("docs_complete", "desk"): {"docs_pending"},
    ("draft", "officer"): {"closed"},
}
```

### 6.4 Presign and download
`GET /v1/documents/{id}/download` checks scope (desk own/unassigned), lifecycle `active|superseded`, `scan_status=clean`, then `presign_get(key, ttl=300, response_content_disposition="attachment; filename*=UTF-8''<sanitised>")` and returns 302. Audit `doc.downloaded` with the user id only.

### 6.5 Document processing sequence
```
Desk -> API: POST /documents            (ClamAV scan, MinIO put, row, audit, SSE document.uploaded)
API -> n8n: intake/document-uploaded    (F1 flow, doc 08)
n8n -> vision-service: quality + stamp  -> API /internal/.../quality
n8n -> doc-pipeline: parse + mask       -> API /internal/.../parse (pass 1)
n8n -> crew: classify + extract         -> API /internal/.../classify, /parse (pass 2)
API: agreement + gates -> parse_status -> SSE document.parsed -> triggers completeness (doc 04)
```

## 7. Config / env vars
| Var | Default | Purpose |
|---|---|---|
| `HOSP_MINIO_ENDPOINT`, `HOSP_MINIO_ACCESS_KEY`, `HOSP_MINIO_SECRET_KEY` | – | MinIO |
| `HOSP_MINIO_BUCKET` | `hospital-docs` | |
| `HOSP_CLAMAV_HOST`, `HOSP_CLAMAV_PORT` | `clamav`, `3310` | |
| `HOSP_MAX_UPLOAD_MB` | `25` | per file |
| `HOSP_MAX_FILES_PER_CASE` | `60` | |
| `HOSP_MAX_PDF_PAGES` | `200` | |
| `HOSP_N8N_WEBHOOK_BASE` | `http://hospital-n8n:5678/webhook` | |
| `HOSP_PRESIGN_TTL_S` | `300` | |
| `HOSP_UPLOAD_RATE_PER_MIN` | `30` | |
| `HOSP_SWEEPER_INTERVAL_S` / `HOSP_SWEEPER_MAX_ATTEMPTS` | `120` / `5` | |
| `HOSP_NAME_MATCH_MIN` | `90` | token-set ratio |
Config domain `confidence_gates` (doc 01 seeds): `classification_min` 0.80, `agreement_min` 0.90, `parse_min` 0.75, `quality_min` 0.5. Config domain `deadlines`: `reimbursement_filing_days` 30.

ClamAV `StreamMaxLength` must be ≥ 26 MB (set in the clamav config owned by `04-shared-services/01`).

## 8. Error handling and edge cases
| Situation | Behaviour |
|---|---|
| ClamAV unreachable | Fail closed: 503 `scan_unavailable`; nothing stored; UI shows retry banner. |
| ClamAV size limit lower than upload limit | Startup check compares config; app refuses to start if `StreamMaxLength < HOSP_MAX_UPLOAD_MB`. |
| EICAR/infected file | Quarantined key, `lifecycle=quarantined`, never downloadable, audit and admin SSE alert. |
| Same file uploaded to two cases | Allowed (hash is unique per case). |
| Same file uploaded twice concurrently | UNIQUE violation caught → treated as duplicate, orphan object deleted. |
| Password-protected PDF | 422 `encrypted_pdf` with a desk-friendly message to request an unlocked copy. |
| PDF with JavaScript/embedded files | Rejected `active_content`. |
| Corrupt PDF/image | 422 `corrupt_file`. |
| Zero-byte or > 25 MB file | 422 `empty_file` / 413 `file_too_large`. |
| Decompression bomb image | 422 `image_too_large`. |
| Multi-document PDF (bill + report in one file) | Classifier marks `mixed`; pipeline calls `/split`; child documents linked via `parent_id` + `page_range`; parent superseded. |
| Phone photos rotated/blurred | vision-service flags `blurry`, `skewed`, `low_light`, `cropped`; sets `illegible_candidate`; completeness treats as present-but-unusable (doc 04). |
| Orphan MinIO objects after DB rollback | Daily orphan scan reports; put happens inside the transaction so a rollback leaves an object — scan reconciles. |
| Unicode / hostile filenames | Sanitised for display (`NFKC`, strip path separators, control chars); stored key is generated. |
| Case locked (status `submitted` onward) | Uploads rejected with 423 `case_locked` unless they are supplementary docs for an open insurer query (doc 07 calls `ingest()` with `purpose="query"`). |
| `supersedes_id` from another case or already superseded | 422 `invalid_supersede`. |
| Patient UHID conflict | 409 `patient_conflict` unless confirmed. |
| Stale `If-Match` | 412. |
| Two desk users edit same case | Second PATCH gets 412; UI reloads. |
| Pass 2 never arrives | Sweeper marks `failed` after 5 attempts; officer can `reparse` or reclassify manually. |
| Manual reclassify of a doc with a completed pipeline | Allowed; stored `typed_json` retained, completeness re-runs. |
| Callback for deleted/quarantined doc | 409 `document_not_active`, ignored. |
| Large concurrent batch (60 files) | Processed with bounded concurrency (4 workers) so ClamAV and MinIO are not flooded. |
| n8n down | Documents stay `pending`; sweeper retries when n8n returns; SSE banner "processing delayed". |
| Presigned URL leak | 5 min TTL, single object, read-only; audit `doc.downloaded`. |

## 9. Tests
**Unit**
| Area | Cases |
|---|---|
| MIME sniff table | pdf, jpg, png, tiff, exe renamed `.pdf`, empty, text renamed `.jpg` |
| Hash/stream | sha256 equals reference, oversize detected mid-stream |
| Duplicate path | same hash same case → skipped; different case → accepted |
| Transition rules | whitelist, forbidden manual transitions, DB trigger fallback |
| Scope filter | desk own/unassigned, officer all |
| Agreement function | equal, minor name variance, amount mismatch, missing keys |
| Classification gate | below/above threshold, manual wins over auto |
| Keyset cursor | stable under concurrent inserts, round-trips |
| Validation | each rule in §4.1 table |

**Integration**
| Scenario | Expected |
|---|---|
| Upload clean PDF | 202, stored, n8n webhook called once |
| EICAR | rejected `infected`, object only under `quarantine/`, download 404 |
| Oversize | 413/422 |
| Encrypted PDF | 422 `encrypted_pdf` |
| Wrong extension (exe as pdf) | 422 `unsupported_media_type` |
| Duplicate / concurrent duplicate | one row, one object |
| MinIO outage mid-upload | transaction rolled back, no row |
| ClamAV down | 503, nothing stored |
| Supersede | old doc lifecycle superseded, excluded from `GET ?lifecycle=active` |
| Delete before/after submission | 204 / 409 |
| Mixed PDF split | children created with page ranges, parent superseded |
| Callbacks | bad payload 422, wrong service role 403, replay idempotent, pass 2 computes agreement |
| Sweeper | pending doc re-triggered, 5th failure marks failed |
| Optimistic lock | stale `If-Match` → 412 |
| Case create | patient conflict 409, claim_ref format, config snapshot stored |
| Rate limit | 31st file in a minute → 429 |

**API contract**: OpenAPI snapshot test; schemathesis against the running app.
**E2E (doc-pipeline stubbed)**: upload → parse pass 1 + 2 → classified → SSE events received in order → completeness trigger fired.
**Load**: case list p95 < 150 ms at 10k cases; upload of 10 mixed files < 5 s (excluding parsing).
Fixtures in `data/synthetic/docs/` (generated by `05-integration-and-eval/01-synthetic-data.md`); EICAR string built at test time (never committed as a file).

Edge-case test matrix (status × action):
| Case status | Upload | Delete doc | Reclassify | PATCH case | Manual transition |
|---|---|---|---|---|---|
| draft | ✔ → docs_pending | ✔ | ✔ | ✔ | cancel only |
| docs_pending | ✔ | ✔ | ✔ | ✔ | → docs_complete (officer) |
| docs_complete | ✔ → docs_pending | ✔ → docs_pending | ✔ | ✔ | → docs_pending |
| building_claim | ✘ 423 | ✘ 409 | ✘ 409 | doctor only | ✘ |
| ready_for_review | ✔ (returns to docs_pending) | ✔ (invalidates sign-off) | ✔ | ✘ | ✘ |
| submitted and later | ✘ 423 (query attachments excepted) | ✘ 409 | ✘ 409 | ✘ | ✘ |

**Property and robustness tests**
| Test | Detail |
|---|---|
| Fuzzed filenames | hypothesis strings incl. `../`, NUL, RTL override, 1000 chars → sanitised, no path escape |
| Chunked upload with lying `Content-Length` | rejected once actual bytes exceed limit |
| Hash race | 20 parallel identical uploads → 1 row, 19 skipped |
| Idempotent callbacks | replay each internal callback twice → single effect, single audit event |
| Cursor stability | insert rows between page fetches → no duplicates/omissions |
| Tombstone purge job | object removed, row kept, `storage_key` nulled, download 410 |
| SSE ordering | `document.uploaded` → `scanned` → `parsed` order preserved per document |
| Config snapshot | case created before a new `doc_requirements` publish keeps old version in `config_versions` |

## 10. Acceptance criteria
- [ ] EICAR upload is quarantined and never reachable by download.
- [ ] 10 mixed files upload in < 5 s (excluding parsing).
- [ ] Every document action produces an audit event with no PII (payload redaction test).
- [ ] Case list p95 < 150 ms with 10k cases.
- [ ] Optimistic-lock conflicts return 412/409 properly.
- [ ] Duplicate upload (sequential and concurrent) yields exactly one row and one object.
- [ ] Sweeper recovers a document whose n8n trigger was dropped within 3 minutes.
- [ ] All endpoints in §4 appear in `openapi.json` with examples; schemathesis run is clean.
- [ ] Deleting or superseding a document triggers a completeness re-run (verified with a stub).

## 11. Dependencies
Docs 01 (schema, all columns in §3.6), 02 (auth, scope, service guards); MinIO, ClamAV, Redis infra (`04-shared-services/01`); n8n webhooks (doc 08) — stub with a fake receiver until ready; doc-pipeline and vision-service contracts (`04-shared-services/02`, `03`); completeness engine trigger (doc 04) — stub with a no-op hook; SSE hub (doc 07) — stub with an in-memory publisher. Consumed by docs 04, 05, 06, 07, 10.

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/03-api-cases-documents.md tasks 1-18 in order. Stub n8n with a local fake webhook receiver, the SSE hub with an in-memory publisher and the completeness trigger with a no-op. Write tests from section 9 as you go; build the EICAR string at test time. Run the matrix tests and the schemathesis check before stopping. If a column you need is missing from the DB, stop and add it to doc 01 section 3.6 and a migration first.
