# 04-02 — doc-pipeline (MinerU + Presidio)

Owner: **Dev A**. Port 8200. Consumers: hospital-crew (Document Intake Agent), insurer-crew (Authenticity/Identity), both APIs. Status: PROPOSED where marked.

## 0. Table of contents
1. Goal · 2. Inputs/Outputs · 3. Data model · 4. API · 5. Build tasks · 6. Key logic (orchestration, stages, gate, agreement, masking guard) · 7. Config · 8. Errors/edge cases · 9. Tests · 10. Acceptance · 11. Dependencies · 12. Kickoff prompt

## 1. Goal
Turn an uploaded document (PDF or image) into **typed, masked, validated JSON** with per-field evidence and deterministic confidence, via:

```
raw file ──► [S1 render/extract] ──► [S2 parse: MinerU or text layer] ──► [S3 classify]
        ──► [S4 entities: Presidio] ──► [S5 mask] ──► [S6 LLM extract pass A (local)]
        ──► [S7 pass B (cloud, masked) when gate says so] ──► [S8 merge + evidence link]
        ──► [S9 validate (code)] ──► [S10 gate + assemble] ──► parsed.json
```
Principles enforced here:
- **Deterministic first (P1):** classification rules, validation arithmetic, checksum checks, and gating are code.
- **Evidence over confidence (P5):** gates use parser confidence, two-pass agreement and code checks, never an LLM's self-reported confidence.
- **Privacy by construction (P4):** raw identifiers never leave the machine; masked text is the only thing sent to any non-local model alias, behind a hard guard (§6.6).
- **Fixable before fatal (P7):** failures yield `needs_review` with reasons, not exceptions.

Non-goals: medical coding, fraud decisions, policy interpretation (those live in crews/APIs).

## 2. Inputs / Outputs
**Input** `POST /v1/parse` with `{system, case_id, doc_id, bucket, key, doc_type_hint?, options?}`. The service reads the object from MinIO using the per-system read user (`docpipe_hosp` or `docpipe_ins`, see infra doc §3.2.3), so a hospital request can never read insurer objects. Multipart upload variant `POST /v1/parse/upload` exists only when `DOCPIPE_ALLOW_UPLOAD=true` (tests).

**Output** `ParsedDocument` JSON, persisted at `{bucket}/{case_id}/{doc_id}/parsed.json` (version tag `pipeline_version`), and the encrypted PII map at `pii_map.enc`. Returned inline (sync) or via job result.

Callers by purpose:
| Caller | Use |
|---|---|
| hospital-crew Document Intake Agent | `/v1/parse` after upload scan, to classify and extract; reads `needs_review`, `fields`, `tables` |
| hospital-api completeness engine | consumes `doc_type`, `doc_type_conf`, `page_quality`, `needs_review` (never raw text) |
| insurer-crew Identity/Authenticity | `/v1/parse` on copied-in docs; reads fields for cross-check |
| rag-service (optional) | `/v1/mask` for any text going to an external embedding or LLM |

## 3. Data model

### 3.1 Pydantic models (`services/doc-pipeline/app/models.py`)
```python
class Span(BaseModel):
    page: int
    bbox: tuple[float, float, float, float]  # x0,y0,x1,y1 normalised 0..1
    text: str
    block_id: str


class FieldValue(BaseModel):
    name: str
    value: str | Decimal | date | None
    raw: str | None  # as extracted, still masked-safe (no PII classes)
    evidence: list[Span]
    parser_conf: float  # 0..1, min over evidence blocks' OCR/layout conf
    pass_a: str | None
    pass_b: str | None  # stringified candidates (masked-safe)
    pass_agreement: bool | None  # None if pass B not run
    validated: bool
    validation_errors: list[str]
    is_critical: bool


class TableCell(BaseModel):
    text: str
    conf: float


class Table(BaseModel):
    page: int
    header: list[str]
    rows: list[list[TableCell]]
    kind: Literal["bill_lines", "lab_results", "medication", "other"]
    totals_row_index: int | None


class MaskedEntity(BaseModel):
    type: Literal[
        "PERSON",
        "AADHAAR",
        "PAN",
        "PHONE",
        "EMAIL",
        "ADDRESS",
        "DOB",
        "POLICY_NO",
        "MEMBER_ID",
        "REG_NO",
        "ACCOUNT_NO",
        "IFSC",
    ]
    token: str  # e.g. <PERSON_1>
    page: int
    count: int  # NEVER the raw value


class PageQuality(BaseModel): ...  # imported from vision-service contract (doc 03)


class Segment(BaseModel):  # multi-document PDFs
    start_page: int
    end_page: int
    doc_type: DocType
    doc_type_conf: float


class ParsedDocument(BaseModel):
    doc_id: UUID
    system: Literal["hospital", "insurer"]
    doc_type: DocType
    doc_type_conf: float
    segments: list[Segment] = []
    pages: int
    handwritten_pages: list[int] = []
    page_quality: list[PageQuality] = []
    text_masked: str  # markdown with tokens
    fields: dict[str, FieldValue]
    tables: list[Table]
    entities_masked: list[MaskedEntity]
    pii_map_ref: str | None  # object key of pii_map.enc
    overall_conf: float
    needs_review: bool
    review_reasons: list[str]
    notes: str | None  # English gloss for non-English text
    pipeline_version: str
    model_aliases_used: list[str]
    timings_ms: dict[str, int]
    source_sha256: str
```
`pii_map.enc`: JSON `{token: raw}` encrypted with AES-256-GCM using `PII_MAP_KEY_B64` (per-system key), nonce stored with ciphertext. Stored ONLY in the owning system's bucket. Never logged, never sent to a gateway.

### 3.2 Extraction schemas per DocType (`app/schemas/`)
Fields marked `*` are critical (money/date/identifier) — they trigger pass B and strict gating.

| DocType | Fields |
|---|---|
| `final_bill` | `bill_number`, `bill_date*`, `patient_name`, `uhid`, `admission_date*`, `discharge_date*`, `gross_amount*`, `discount_amount*`, `net_amount*`, `advance_paid*`, `patient_payable*`, `insurer_payable*`, `hospital_name`, `hospital_reg_no`, table `bill_lines` |
| `itemised_bill` | table `bill_lines` (code, description, category, qty, rate*, amount*), `total*` |
| `pharmacy_bill` | `bill_number`, `bill_date*`, `total*`, table `medication` |
| `discharge_summary` | `patient_name`, `age`, `gender`, `uhid`, `admission_date*`, `discharge_date*`, `diagnosis_text`, `icd_codes[]`, `procedures[]`, `treating_doctor`, `doctor_reg_no`, `condition_at_discharge`, `follow_up` |
| `lab_report` / `radiology_report` / `investigation_report` | `report_date*`, `patient_name`, `test_names[]`, `ordering_doctor`, `lab_name`, table `lab_results` |
| `admission_note` | `admission_date*`, `admission_type`, `presenting_complaint`, `provisional_diagnosis` |
| `preauth_approval` | `preauth_ref*`, `approved_amount*`, `valid_till*`, `insurer_name`, `member_id*` |
| `claim_form` | `policy_number*`, `member_id*`, `patient_name`, `dob*`, `claim_amount*`, `bank_account_masked`, `signature_present` |
| `id_proof` | `id_type`, `id_number*` (masked immediately, only hash returned: `id_number_sha256`), `name`, `dob*` |
| `policy_card` | `policy_number*`, `member_id*`, `insurer_name`, `valid_from*`, `valid_to*` |
| `cancelled_cheque` | `account_holder`, `account_no_masked`, `ifsc*`, `bank_name` |
| `implant_sticker` | `implant_name`, `manufacturer`, `serial_no*`, `mrp*`, `batch_no` |
| `payment_receipt` | `receipt_no`, `date*`, `amount*`, `mode` |
| `fir_mlc` | `mlc_no`, `date*`, `police_station` |
| `other` | free `title`, `summary` |

Schemas are Pydantic classes generating JSON Schema for the LLM `response_format`; `critical` flags live in a `CRITICAL_FIELDS: dict[DocType, set[str]]` table.

## 4. API / endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/parse` | Sync when pages ≤ `DOCPIPE_SYNC_MAX_PAGES` (3) and estimated time < 90 s; otherwise 202 `{job_id}` |
| GET | `/v1/jobs/{job_id}` | `{status: queued|running|done|failed, result?, error?}` |
| POST | `/v1/classify` | classification only (fast path, no LLM unless rules fail) |
| POST | `/v1/mask` | `{text, system}` → `{masked_text, entities[]}`; no map persisted unless `persist_map=true` with `doc_id` |
| POST | `/v1/unmask` | local-only, requires service role `svc-internal` + `system`; `{doc_id, text}` → raw text; used by hospital UI for officer review; every call audited |
| GET | `/v1/health`, `/v1/version` | health, pipeline version, model aliases, MinerU version |

Auth: bearer JWT from the calling system's realm with role `svc-crew`, `svc-internal` or `svc-n8n`; `system` claim must equal `req.system` (prevents a hospital token reading insurer objects).

### 4.1 Examples
Request:
```json
POST /v1/parse
{"system":"hospital","case_id":"5b1f...","doc_id":"c0a8...","bucket":"hospital-docs",
 "key":"5b1f.../c0a8.../original.pdf","doc_type_hint":"final_bill","options":{"force_second_pass":false}}
```
Sync response 200 (abridged):
```json
{"doc_id":"c0a8...","doc_type":"final_bill","doc_type_conf":0.96,"pages":2,"overall_conf":0.91,
 "fields":{
  "gross_amount":{"value":"182450.00","parser_conf":0.93,"pass_agreement":true,"validated":true,
                  "evidence":[{"page":2,"bbox":[0.61,0.72,0.88,0.75],"text":"182,450.00","block_id":"p2b17"}],"is_critical":true},
  "patient_name":{"value":"<PERSON_1>","parser_conf":0.97,"validated":true,"is_critical":false}},
 "tables":[{"page":1,"kind":"bill_lines","header":["Description","Qty","Rate","Amount"],"rows":[...]}],
 "needs_review":false,"review_reasons":[],"pipeline_version":"1.0.3","timings_ms":{"parse":8200,"llm_a":4100,"llm_b":2700,"validate":40}}
```
Note `patient_name` returns the TOKEN to callers by default; callers who need the real value (the hospital officer UI) call `/v1/unmask` through hospital-api, which checks role and audits.

Async 202:
```json
{"job_id":"j-7d1...","status":"queued","poll":"/v1/jobs/j-7d1..."}
```
Error 422 (problem+json): `{"code":"encrypted_pdf","detail":"Document is password protected"}`; 413 `too_many_pages`; 404 `object_not_found`; 503 `llm_unavailable` only when even parser-only output cannot be produced (rare).

### 4.2 Idempotency/caching
Key `(doc_id, source_sha256, pipeline_version, options_hash)`. A hit returns the stored `parsed.json` without recomputation; `options.force=true` bypasses.

## 5. Build tasks
Each task lists files and a definition of done.

1. **Scaffold** `services/doc-pipeline/`: `pyproject.toml` (fastapi, uvicorn, pydantic v2, httpx, boto3, pymupdf, mineru, presidio-analyzer, presidio-anonymizer, spacy, rapidfuzz, cryptography, json-repair, tenacity, redis, orjson), `Dockerfile` (python 3.12-slim, MinerU `pipeline` backend CPU, models downloaded at build into `/models`, spaCy `en_core_web_lg` or `en_core_web_md` PROPOSED md for RAM, non-root), `app/main.py`, `app/settings.py`, `app/auth.py` (copy of infra helper pattern, roles `svc-*`). DoD: `/v1/health` returns 200, container ≤ 3 GB.
2. **Storage** `app/storage.py`: read objects by system (two boto clients created lazily), write `parsed.json` and `pii_map.enc`, tag `scan` check (refuse objects whose tag `scan != clean` with 409 `unscanned_object`). DoD: unit tests with moto.
3. **Render/extract** `app/stages/render.py`: detect type via magic bytes; PDF → PyMuPDF per-page text layer + page PNGs at 200 dpi saved to `pages/{n}.png` (needed by vision-service); images → normalise orientation (EXIF), convert to RGB PNG. Text-layer sufficiency rule (PROPOSED): page uses text layer when `len(text) > TEXTLAYER_MIN_CHARS (200)` and printable ratio ≥ 0.95 and the text is not an OCR-garbage page (dictionary-word ratio ≥ 0.5 via a small English+Hindi word list); else OCR via MinerU.
4. **Parse** `app/stages/parse.py`: MinerU wrapper producing markdown, layout blocks with bbox and confidence, tables as grids. For text-layer pages synthesise blocks from PyMuPDF `get_text("dict")` with `conf=0.99`. Normalise to `ParseOut(blocks, tables, text, page_conf[])`. Table post-processing: merge rows split across lines, detect header row, detect totals row (regex `(?i)^(sub)?\s*total|grand total|net amount`).
5. **Classify** `app/stages/classify.py`: rule engine from `config/classify_rules.yaml` (keywords with weights per DocType; header position boost), producing score per type; accept if top score ≥ 0.75 and margin ≥ 0.2; else LLM fallback (alias `cleanup-local`, masked first 1500 chars, JSON `{doc_type, reason}`). Honour `doc_type_hint` as a prior (+0.15) not an override. Page-level classification when `pages > 1` for segmenting merged PDFs.
6. **Entities** `app/stages/entities.py`: Presidio `AnalyzerEngine` with custom recognizers:
   - `AadhaarRecognizer`: regex `\b\d{4}\s?\d{4}\s?\d{4}\b` + Verhoeff checksum (score 0.9 if valid, 0.3 if not → still masked when context words present).
   - `PanRecognizer`: `\b[A-Z]{5}\d{4}[A-Z]\b`.
   - `IndianPhoneRecognizer`: `(?:\+91[\s-]?)?[6-9]\d{9}`.
   - `PolicyNoRecognizer`, `MemberIdRecognizer`: patterns from `config/id_patterns.yaml` (editable; per insurer).
   - `RegNoRecognizer` (hospital/doctor registration numbers).
   - `IfscRecognizer`, `AccountNoRecognizer` (9-18 digits with context words "A/c", "Account").
   - Built-ins: PERSON, EMAIL_ADDRESS, DATE_TIME (only when near "DOB"/"Date of birth"/"born"), LOCATION limited to address-like contexts.
   - **Not masked (allowlist `config/allowlist.txt`):** ICD-10 codes, drug names, hospital names, doctor titles, test names, CPT/procedure codes, bill numbers/dates (dates are masked only as DOB).
   DoD: precision/recall on `data/synthetic/pii_labels.jsonl` ≥ 0.98 recall for AADHAAR/PAN/PHONE.
7. **Mask** `app/stages/mask.py`: replace entities with stable tokens `<TYPE_n>` per distinct value (same person → same token), keep a reversible map, AES-GCM encrypt and store. Preserve layout (token replaces the span; line breaks untouched). Return entity summary without raw values.
8. **LLM extract** `app/stages/extract.py`: calls the llm-gateway (OpenAI-compatible) with a system prompt per doc type (`prompts/{doc_type}.md`, versioned, ids recorded in `model_info`), `response_format` = JSON Schema, `temperature=0`, aliases `cleanup-local` (Pass A) and `cleanup-cloud` (Pass B). Input = masked text plus table markdown, chunked by page if > 6000 tokens (map-reduce for multi-page: extract per page-chunk, merge by field priority rules). Each call is guarded by §6.6.
9. **Merge + evidence** `app/stages/merge.py`: field-level merge of A and B (rules §6.4), link evidence (§6.2).
10. **Validate** `app/stages/validate.py`: deterministic rules (§6.5).
11. **Gate + assemble** `app/stages/gate.py`: §6.3 produces `needs_review`, `review_reasons`, `overall_conf`.
12. **Jobs** `app/jobs.py`: Redis stream `docpipe:jobs` (consumer group `workers`), worker coroutine count `DOCPIPE_WORKERS` (default 2), per-job timeout 300 s, retry 1 time on transient failures, DLQ stream `docpipe:dead`. Result stored at `docpipe:result:{job_id}` for 1 h.
13. **Endpoints** `app/api/*.py` including `/v1/mask` and `/v1/unmask` (audit hook callback to the caller's API via `X-Audit-Callback` not used; instead `unmask` writes an access log line with `request_id`, `actor`, `doc_id` to the structured log which hospital-api mirrors into audit — PROPOSED).
14. **Observability** `app/obs.py`: Langfuse spans for LLM calls with masked prompts only; Prometheus-style `/metrics` (stage latency histograms, gate outcomes).
15. **Tests + golden corpus** per §9, plus a CLI `python -m app.cli parse file.pdf --system hospital` for local dev.

## 6. Key logic

### 6.1 Orchestration (reference implementation)
```python
async def parse(req: ParseRequest) -> ParsedDocument:
    t = Timer()
    raw = await storage.get(req.system, req.bucket, req.key)  # also checks scan tag
    sha = sha256(raw)
    if cached := await cache.get(req.doc_id, sha, PIPELINE_VERSION, req.options):
        return cached
    pages = render.extract(raw)  # raises EncryptedPdf / TooManyPages
    await storage.put_pages(req, pages)  # PNGs for vision-service
    parse_out = await run_in_pool(parse.run, pages)  # CPU heavy; process pool (size 1-2)
    types = classify.run(parse_out, req.doc_type_hint)  # segments + doc_type
    ents = entities.analyze(parse_out.text)
    masked, pii_map = mask.apply(parse_out, ents)
    guard.assert_safe(masked, pii_map)  # §6.6 (raises -> local-only mode)
    schema = SCHEMAS[types.primary]
    a = await extract.run(masked, schema, alias="cleanup-local", tables=masked.tables)
    need_b = gate.needs_second_pass(parse_out, a, schema, req.options)
    b = (
        await extract.run(masked, schema, alias="cleanup-cloud", fields=need_b.fields)
        if need_b and guard.cloud_allowed
        else None
    )
    fields = merge.run(a, b, parse_out)
    fields = validate.run(fields, parse_out.tables, schema)
    result = gate.assemble(req, types, fields, parse_out, ents, timings=t.done())
    await storage.put_parsed(req, result, pii_map)
    await cache.set(...)
    return result
```
Heavy CPU stages run in a `ProcessPoolExecutor(max_workers=1)` so the event loop stays responsive; concurrency across documents is bounded by a semaphore (`DOCPIPE_WORKERS`).

### 6.2 Evidence linking
For each extracted value `v`:
1. Normalise: strip currency symbols/commas for numbers, ISO-ize dates, lowercase and collapse whitespace for text.
2. Candidate blocks = blocks on pages hinted by the extractor (page index returned in the schema `page` hint) else all blocks.
3. Score by `rapidfuzz.fuzz.partial_ratio` (text) or exact numeric equality after normalisation (numbers/dates); accept ≥ 90 (text) or exact (numbers).
4. Evidence = top-1 block (top-2 for table cells). `parser_conf = min(block.conf)`.
5. No acceptable block → `evidence=[]`, `validated=False`, error `no_evidence`. For masked tokens (e.g. `<PERSON_1>`), the token text itself exists in `text_masked`, so linking works on masked text and bbox is taken from the original block.

### 6.3 Gate (`needs_review`)
Computed in code only. `needs_review=True` with reasons if ANY:
| Reason code | Condition |
|---|---|
| `low_parser_conf` | `overall_conf < confidence_gates.parser_min` (hospital config, default 0.80); `overall_conf` = weighted mean of critical fields' `parser_conf` (weight 2) and others (weight 1) |
| `missing_required` | a required field for the DocType (config `doc_requirements`) is absent or has no evidence |
| `critical_disagreement` | pass A ≠ pass B on a critical field (§6.4) |
| `validation_error` | any `validation_errors` on a critical field |
| `bad_page_quality` | vision-service marks any page `legible=False` (when `page_quality` supplied/available) |
| `handwritten` | handwriting detected on pages containing critical fields |
| `low_classification` | `doc_type_conf < 0.75` or segments disagree with hint |
| `llm_unavailable` | extraction ran parser-only / rules-only |
| `masking_guard_tripped` | cloud pass skipped due to guard; result is local-only (informational, does not alone set review) |
| `table_total_mismatch` | bill lines sum ≠ total (validation) |
`overall_conf` itself never includes any LLM-reported number.

Handwriting detection (PROPOSED, deterministic): MinerU/PaddleOCR line-level recognition confidence < 0.6 with irregular baseline variance (stddev of text-line angle > 4°) or character width variance high; flagged per page.

### 6.4 Two-pass agreement and merge
Pass A = local model on masked text for ALL fields. Pass B = cloud model on masked text for a subset:
- Run B when: `options.force_second_pass`, OR any critical field has `parser_conf < 0.90`, OR pass A produced a validation error on a critical field, OR doc is "high value" (`net_amount ≥ ₹1,00,000` as detected by A or `claim_form`/`final_bill` types) — PROPOSED to conserve free-tier quota.
- B receives only the critical fields subset (smaller prompt, cheaper), plus table markdown.
Comparison per field type:
| Type | Agreement rule |
|---|---|
| Money | `Decimal` equality after normalisation (exact) |
| Date | equal ISO dates |
| Identifier (policy/member/preauth/serial) | equal after uppercasing and stripping spaces/hyphens |
| Names / text | `token_set_ratio ≥ 90` |
| Lists (icd_codes) | set equality; partial overlap → disagreement listing diff |
Merge decision:
1. Agree → value = A, `pass_agreement=True`.
2. Disagree → choose the candidate that has evidence match and passes validation; if both or neither → value = candidate with evidence of higher `parser_conf`, `pass_agreement=False`, `validated=False`, add `critical_disagreement` reason. The not-chosen candidate is kept in `pass_a/pass_b` fields for the reviewer UI.
3. B not run → `pass_agreement=None`; critical fields still gated by parser_conf and validation.
Deterministic cross-check shortcut: for amounts, a candidate that equals a number present in a table totals row is preferred.

### 6.5 Validation rules (`validate.py`)
| Rule id | Check | Result |
|---|---|---|
| V01 | `sum(bill_lines.amount) == subtotal` within ₹0.01 (and per-category subtotals when present) | `table_total_mismatch` |
| V02 | `gross - discount == net` | error on `net_amount` |
| V03 | `net - advance == patient_payable + insurer_payable` (when both present) | error |
| V04 | `qty * rate == amount` per line within ₹0.01 | line-level error |
| V05 | `admission_date <= discharge_date <= today`; stay ≤ 365 days | error |
| V06 | bill_date ≥ admission_date and ≤ discharge_date + 30 days | warning |
| V07 | amount regex `^\d{1,9}(\.\d{1,2})?$` after normalisation, non-negative | error |
| V08 | ICD-10 regex `^[A-TV-Z]\d{2}(\.\d{1,4})?$` | warning (unknown but well-formed allowed) |
| V09 | Aadhaar Verhoeff (when an ID field is present, validated pre-mask) | error `invalid_id_checksum` |
| V10 | PAN structure | error |
| V11 | IFSC `^[A-Z]{4}0[A-Z0-9]{6}$` | error |
| V12 | policy validity dates `valid_from < valid_to` | error |
| V13 | currency symbol/words consistent (`₹`/`Rs`/`INR`) | warning |
| V14 | duplicate line items (same description+amount twice) | warning `possible_duplicate_line` |
| V15 | totals words vs figures ("Rupees one lakh ...") when words present | warning |
Decimal arithmetic only (`Decimal`, quantize `0.01`, ROUND_HALF_UP); never float.

### 6.6 Masking guard (hard stop before any non-local call)
```python
def assert_safe(masked: MaskedText, pii_map: dict[str, str]) -> None:
    for raw in pii_map.values():
        if len(raw) >= 4 and raw in masked.text:
            raise GuardTripped("raw_value_present")
    rescan = presidio.analyze(
        masked.text, entities=["AADHAAR", "PAN", "PHONE", "EMAIL", "ACCOUNT_NO", "IFSC"]
    )
    if any(e.score >= 0.5 for e in rescan):
        raise GuardTripped("rescan_found_pii")
    if looks_like_unmasked_name_block(masked):
        raise GuardTripped(
            "name_heuristic"
        )  # e.g. "Patient Name:" followed by capitalised words not a token
```
On `GuardTripped`: no external call; run local alias only; append `masking_guard_tripped`; write a security event (`guard.tripped`, reason, doc_id, no content). Additionally the gateway client has a **last-mile filter**: regexes for Aadhaar/PAN/phone applied on the outbound JSON body for alias families `*-cloud` and `vision-cloud`; any hit aborts the request and raises `GuardTripped`. Test proxy (§9) validates this independently.

### 6.7 Multi-page and multi-document handling
- Classify each page; consecutive pages with the same type and a continuity cue (page "x of y", same header) form a segment. Segments become separate extractions; `ParsedDocument.fields` represent the primary segment; additional segments are returned in `segments` with their own `fields` under `segment_results[]` (PROPOSED extension: array of ParsedDocument-lite). hospital-api may split the upload into child documents (`document.parent_id`, hospital doc 03).
- Long documents (> 6000 tokens): per-page-chunk extraction, then merge per field: pick the value from the page with the best evidence match; lists concatenated and de-duplicated.

### 6.8 Hindi/regional language
OCR langs `en,hi` (PaddleOCR language packs). LLM instructions: keep original script in values, no translation; put an English gloss in `notes` only. Presidio runs on English NER; for Devanagari the regex-based recognizers (Aadhaar/PAN/phone/IDs) still apply; names in Devanagari are masked via a gazetteer-free heuristic: tokens following "नाम" / "Name" label patterns (config list `config/labels_hi.yaml`). Documented limitation: Devanagari person-name recall is lower; any Devanagari block that precedes a label from the list and contains no token is masked entirely (over-masking is acceptable).

## 7. Config / env vars
| Var | Default | Meaning |
|---|---|---|
| `DOCPIPE_MAX_PAGES` | 40 | reject larger |
| `DOCPIPE_SYNC_MAX_PAGES` | 3 | sync threshold |
| `DOCPIPE_WORKERS` | 2 | concurrent documents |
| `DOCPIPE_ALLOW_UPLOAD` | false | multipart test endpoint |
| `MINERU_BACKEND` | pipeline | CPU |
| `TEXTLAYER_MIN_CHARS` | 200 | text-layer sufficiency |
| `PARSER_MIN_CONF` | 0.80 | fallback if config service unreachable |
| `SECOND_PASS_PARSER_CONF` | 0.90 | run pass B below this on critical fields |
| `HIGH_VALUE_THRESHOLD_INR` | 100000 | pass B trigger |
| `OCR_LANGS` | en,hi | |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | | virtual key only |
| `ALIAS_LOCAL`, `ALIAS_CLOUD` | cleanup-local, cleanup-cloud | |
| `PII_MAP_KEY_B64_HOSP`, `PII_MAP_KEY_B64_INS` | | per-system AES keys |
| `MINIO_*` read users | | `docpipe_hosp`, `docpipe_ins` |
| `REDIS_URL` | | user `docpipe` |
| `LLM_TIMEOUT_S` | 60 | per call |
| `PIPELINE_VERSION` | from git tag | included in cache key |
Config rows from hospital-api (`confidence_gates`, doc requirements) are fetched via `GET /internal/config/confidence-gates` using the `hospital-internal` client; cached 60 s; fall back to env defaults when unreachable (logged).

## 8. Error handling and edge cases
| # | Case | Behaviour |
|---|---|---|
| 1 | Encrypted/password PDF | 422 `encrypted_pdf`; hospital-api marks doc `needs_reupload` |
| 2 | Corrupt PDF | 422 `unreadable_document`; try image conversion once via `pdf2image`; else fail |
| 3 | > `DOCPIPE_MAX_PAGES` | 413 `too_many_pages` |
| 4 | Scanned low-res image | OCR proceeds; conf low → `needs_review`; vision-service quality check recommended re-scan |
| 5 | Multi-document PDF | segments (§6.7) |
| 6 | Handwritten sections | `handwritten_pages`, forced review for critical fields on those pages |
| 7 | Hindi/regional text | §6.8 |
| 8 | LLM returns invalid JSON | one repair attempt (`json-repair` + schema validation); on failure retry once with `temperature 0` and stricter prompt; then rules-only fields (regex extraction for dates/amounts) and `needs_review` |
| 9 | LLM returns fields not in schema | dropped; logged |
| 10 | LLM hallucinated value without evidence | `validated=False`, `no_evidence`, forces review for critical fields |
| 11 | LLM gateway down | local alias only; if local down too → parser-only output + `llm_unavailable` |
| 12 | Cloud rate limit (429) | gateway handles backoff/fallback; pipeline treats `429` after gateway fallback as cloud unavailable → local only, no retry storm |
| 13 | Time budget | 90 s sync; beyond → 202 job; job timeout 300 s then `failed` with parser-only partial result stored |
| 14 | Presidio false positives on drug names/ICD | allowlist (`config/allowlist.txt`) plus context-score threshold 0.5; additional unit tests with a drug list |
| 15 | Presidio false negatives | guard rescan, plus over-masking heuristics near labels; metrics tracked in eval harness |
| 16 | Same token for different people | token map is per-document, counter increments for distinct normalised values |
| 17 | Mask map key rotation | `pii_map.enc` header has `kid`; keys list `PII_MAP_KEYS` (new first); re-encryption job `python -m app.cli rotate` |
| 18 | Idempotent re-run | cache by `(doc_id, sha256, pipeline_version, options_hash)`; new pipeline version re-parses on request |
| 19 | Object not scanned | storage refuses: 409 `unscanned_object` |
| 20 | Memory pressure (OOM) | worker count 1 fallback via `DOCPIPE_WORKERS`; MinerU run in a subprocess recycled every 20 docs; container `mem_limit: 3g` |
| 21 | Large images | downscale to 3000 px longest side before OCR |
| 22 | Rotated pages | PaddleOCR angle classifier; MinerU orientation fix; OSD via tesseract-free heuristic |
| 23 | Currency in words only | V15 + extraction schema includes `amount_in_words`; mismatch is warning |
| 24 | Duplicate/stamped overlays obscuring numbers | OCR low conf → review |
| 25 | Concurrency on same doc | Redis lock `lock:docpipe:{doc_id}`; second caller waits (max 90 s) then reads cache |

## 9. Tests

### 9.1 Golden corpus
`data/synthetic/docs/` created by the synthetic generator (05-integration/01): ReportLab PDFs with ground-truth JSON, 20 per DocType, in 5 degradation levels (clean, noisy, rotated, low-res, photographed with perspective). Targets:
| Metric | Target |
|---|---|
| Doc-type accuracy | ≥ 0.97 clean, ≥ 0.92 degraded |
| Money/date field exact match | ≥ 0.95 clean, ≥ 0.85 degraded |
| Identifier field exact match | ≥ 0.93 |
| `needs_review` recall on deliberately corrupted docs (wrong totals, blurred critical region) | ≥ 0.95 |
| `needs_review` rate on clean docs | ≤ 0.10 |
| PII recall (AADHAAR/PAN/PHONE) | ≥ 0.98; PERSON ≥ 0.90 |
| Raw PII in any outbound gateway body | 0 |

### 9.2 Unit tests
| Area | Cases |
|---|---|
| Verhoeff | 10 valid + 10 invalid Aadhaar numbers incl. spaced/hyphenated |
| PAN/IFSC/phone regexes | positive and negative tables |
| Masking | round-trip (mask → unmask equals original), stable tokens, no raw in `text_masked`, repeated names share token, overlapping entities |
| Allowlist | ICD codes `K35.80`, drug "Paracetamol 500mg" not masked |
| Text-layer rule | digital PDF skips OCR; scanned goes to MinerU |
| Classification | rule table, hint prior, tie → LLM fallback (mock) |
| Evidence linking | exact, fuzzy 92, below threshold, numeric normalisation `1,82,450.00` vs `182450` |
| Validation V01-V15 | each with passing and failing inputs; decimal rounding edge cases (0.005) |
| Agreement | money equal/unequal, date formats, name reorder "Rao S" vs "S Rao", list overlap |
| Gate | table-driven matrix: (conf, missing, disagreement, quality, handwritten) → expected `needs_review` and reason set |
| JSON repair | trailing commas, truncated output, markdown fences |
| Guard | planted raw Aadhaar in masked text trips; name heuristic trips; clean text passes |

### 9.3 Property tests (hypothesis)
`mask(text)` contains no substring of any raw entity ≥ 4 chars; `unmask(mask(text)) == text`; validation never raises for arbitrary field dicts.

### 9.4 Integration
- Mock gateway (respx) returning scripted JSON; scenarios: both passes agree; disagree on amount; pass B timeout; invalid JSON then valid; gateway 503.
- Test proxy in front of the mock gateway records every request body for `*-cloud`; assertion: regexes for Aadhaar/PAN/phone/names from the labels file never appear.
- Redis stream job lifecycle: enqueue, worker consume, retry, DLQ.
- Storage permissions: `docpipe_hosp` cannot read `insurer-docs`.
- End-to-end with the real llm-gateway + Ollama in a nightly job (not per PR).

### 9.5 Fault injection
Kill MinerU subprocess mid-parse; slow LLM (timeout); Redis outage (sync mode still works, async returns 503 `queue_unavailable`).

### 9.6 Performance
3-page text PDF < 8 s; 3-page scanned PDF < 25 s CPU (reference: 8 vCPU, 16 GB); peak RSS < 3 GB; 10 concurrent requests with `WORKERS=2` do not OOM (queueing observed).

## 10. Acceptance criteria
- [ ] All targets in §9.1 met on the golden corpus.
- [ ] Zero raw PII in any outbound cloud request (proxy capture across the whole corpus run).
- [ ] `needs_review` triggers on every deliberately corrupted document.
- [ ] `parsed.json` and `pii_map.enc` written; `pii_map.enc` unreadable without the key (test with wrong key).
- [ ] Sync path ≤ 90 s, async path functional with retries and DLQ.
- [ ] Cross-system isolation: insurer token cannot parse hospital objects (403).
- [ ] Container memory ≤ 3 GB in the perf test.
- [ ] Every stage has latency metrics visible at `/metrics`; Langfuse traces contain masked prompts only.

## 11. Dependencies
- Infra (04-01): MinIO read users and buckets, Redis `docpipe` user, Keycloak service roles.
- llm-gateway (04-04): aliases `cleanup-local`, `cleanup-cloud`, virtual key for `doc-pipeline`.
- vision-service (04-03): `PageQuality` model, optional call after render.
- Shared contract 01-02: `DocType` enum; 01-03: `confidence_gates` config domain; 01-04: audit event names `doc.parsed`, `doc.classified`, `guard.tripped` (written by callers).
- Synthetic data (05-01) and eval harness (05-02) for corpus and metrics.
- Hospital docs 03 (documents) and 04 (completeness) consume outputs; hospital crew doc 09 (Document Intake Agent) calls this service.

## 12. Claude Code kickoff prompt
> Read docs/implementation/04-shared-services/02-doc-pipeline.md and 01-shared-contract/02-data-models-and-enums.md. Implement tasks 1-15 in order. Do the LLM-free stages first (render, parse, classify rules, entities, mask, validate, gate) with unit tests and the golden corpus subset; then add extract/merge against a mocked gateway; then jobs and endpoints. Never log document text, field values, or the PII map; log ids and counts only. Add the outbound-body proxy test before enabling the cloud alias. Stop at the acceptance criteria and report which are checked.
