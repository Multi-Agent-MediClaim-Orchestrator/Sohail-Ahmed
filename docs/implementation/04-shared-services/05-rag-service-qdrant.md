# 04-05 — rag-service (LlamaIndex) and Qdrant

Owner: **Dev B**. rag-service port 8400, Qdrant 6333 (REST) / 6334 (gRPC). Status: PROPOSED where marked.

Related docs: `04-04` (gateway aliases `embed`, `reason-cloud`), `04-02` (doc-pipeline `/v1/parse`), `01-shared-contract/03-config-versioning.md` (policy versions and effective dates), `03-dev-B-insurer/08-crew-agents.md` (Coverage agent, query draft agent), `02-dev-A-hospital/09-crew-agents.md` (Query Responder), `05-integration-and-eval/01-synthetic-data.md` (synthetic policy corpus), `05-integration-and-eval/02-evaluation-harness.md` (RAG metrics).

---

## 1. Goal

Grounded retrieval with citations for two uses, with strict separation between the hospital and insurer sides:

1. **Insurer**: Coverage agent and calc-mapper look up policy wording, exclusions, sub-limit schedules, treatment guidelines and past (synthetic) precedents. Output is passages with citations; the coverage decision itself is made by code and humans (design principle 1 and 3). RAG explains and evidences.
2. **Hospital**: Query Responder drafts replies to insurer queries, citing (a) the hospital's own case documents and (b) the insurer's documented requirements/FAQs held in `hosp_insurer_rules`.

Principles applied:
- **Evidence over confidence** (principle 5): no answer without retrieved evidence; the service returns `insufficient_evidence=true` rather than hallucinating.
- **Config over code** (principle 2): policy passages carry `version`, `effective_from/to` so the retrieval is as of the claim date.
- **Privacy by construction** (principle 4): hospital per-case context holds masked text only and is deleted at case close.
- **Everything auditable** (principle 6): every search/answer call returns a `retrieval_id` that agents store in audit event payloads so a decision can be traced to passages.

Non-goals: no free-form chatbot, no training/fine-tuning, no cross-tenant sharing, no web crawling.

---

## 2. Inputs / Outputs

### 2.1 Inputs
- Source documents in MinIO bucket `kb-sources`, path `kb-sources/{collection}/{doc_slug}/{version}/{filename}` (PDF, DOCX via doc-pipeline, MD, TXT, HTML, CSV for rate tables).
- Per-case parsed text pushed by hospital-api/hospital-crew for `hosp_case_context` (already Presidio-masked).
- Search/answer requests from crews, authenticated by service key (JWT or LiteLLM-virtual-key-style header; see 4.2 for ACL).
- Event `case.closed` (from hospital-n8n) triggering ephemeral data deletion.

### 2.2 Outputs
- Ranked passages: `{citation_id, text, score, source, page, section, version, doc_id}`.
- Grounded answers: `{answer, citations[], insufficient_evidence, retrieval_id}`.
- Ingestion job statuses and ingestion reports (chunk counts, skipped duplicates).
- Prometheus metrics and Langfuse traces (via gateway) for embedding/answer calls.

### 2.3 Caller → collection access

| Caller (service key) | May read | May write/ingest |
|---|---|---|
| `insurer-crew` | `ins_policy_wording`, `ins_medical_guidelines`, `ins_case_history` | none |
| `insurer-api` / admin | same + admin ops | ingest into `ins_*` (admin role) |
| `hospital-crew` | `hosp_insurer_rules`, `hosp_case_context` (own `case_id` only) | `hosp_case_context` only |
| `hospital-api` / admin | same + admin ops | `hosp_*` |
| `eval-harness` | all (read) | test collections `eval_*` only |

Cross-system access is denied in rag-service (not just Qdrant): hospital keys cannot name an `ins_*` collection.

---

## 3. Collections and data model (PROPOSED)

### 3.1 Collection catalogue

| Collection | System | Content | Lifecycle |
|---|---|---|---|
| `ins_policy_wording` | insurer | policy wordings, riders, exclusions, definitions, sub-limit schedules | versioned, `effective_from/to` |
| `ins_medical_guidelines` | insurer | treatment guidelines, package-rate tables, expected length-of-stay norms, day-care procedure lists | versioned |
| `ins_case_history` | insurer | past synthetic decisions with rationale for precedent | append-only; created from closed cases (no raw IDs) |
| `hosp_insurer_rules` | hospital | insurer/TPA documentation requirements, FAQs, forms checklists | versioned |
| `hosp_case_context` | hospital | parsed (masked) documents of **open** cases | ephemeral, deleted at `case.closed` (+7 d grace, PROPOSED) |
| `eval_*` | shared | evaluation fixtures | wiped by eval harness |

### 3.2 Qdrant collection configuration

Each collection:
```json
{
  "vectors": {"dense": {"size": 768, "distance": "Cosine", "on_disk": false}},
  "sparse_vectors": {"bm25": {"index": {"on_disk": false}, "modifier": "idf"}},
  "hnsw_config": {"m": 16, "ef_construct": 100},
  "optimizers_config": {"default_segment_number": 2},
  "quantization_config": {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": true}},
  "on_disk_payload": true
}
```
Rationale: 768d × float32 ≈ 3 KB/vec; int8 scalar quantisation keeps the whole KB in < 1 GB RAM for ~100k chunks, matching the `mem_limit 1g` budget.

### 3.3 Payload schema (all collections)

```json
{
  "doc_id": "uuid",                    "doc_slug": "healthplus-a-wording",
  "source": "HealthPlus-A wording v3", "version": 3,
  "page": 12,                          "page_end": 12,
  "section": "4.2",                    "section_title": "Room rent limits",
  "chunk_index": 41,                   "chunk_type": "text|table|list|definition",
  "system": "insurer|hospital",        "collection": "ins_policy_wording",
  "policy_product": "HealthPlus-A",    "policy_version": "3",
  "effective_from": "2026-01-01",      "effective_to": null,
  "text_hash": "sha256:...",           "token_count": 540,
  "embed_model": "nomic-embed-text@768",
  "caption": "Table of ICU and room rent sub-limits by sum insured",   // tables only
  "case_id": "uuid",                   // hosp_case_context only
  "doc_type": "discharge_summary",     // hosp_case_context only
  "language": "en",
  "created_at": "2026-10-06T10:00:00Z"
}
```
Point id: UUIDv5 of `(collection, doc_id, version, chunk_index)` — deterministic so re-ingestion upserts rather than duplicates.

Payload indexes created at bootstrap: `system` (keyword), `policy_product` (keyword), `policy_version` (keyword), `effective_from` / `effective_to` (datetime), `case_id` (keyword), `doc_id` (keyword), `chunk_type` (keyword), `doc_type` (keyword).

### 3.4 Collection metadata record

rag-service keeps `rag_meta` in its own small SQLite/Postgres table:
```
collection_meta(collection PK, embed_model, embed_dim, created_at, last_reindex_at, chunk_tokens, chunk_overlap)
ingest_job(id, collection, bucket, key, status queued|running|done|failed, chunks_added, chunks_skipped, error, created_at, finished_at)
retrieval_log(retrieval_id PK, caller, collection, query_hash, top_ids JSONB, scores JSONB, created_at)   -- no raw query text if it may contain PII; store hash + masked query
```

---

## 4. API

### 4.1 Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/ingest` | `{collection, bucket, key, metadata}` → `{job_id}` (async) |
| POST | `/v1/ingest/text` | direct text ingestion for `hosp_case_context` (`{collection:"hosp_case_context", case_id, doc_id, doc_type, text, pages[]}`) |
| GET | `/v1/ingest/{job_id}` | job status |
| POST | `/v1/search` | hybrid retrieval + optional rerank |
| POST | `/v1/answer` | retrieval + grounded answer with citations |
| DELETE | `/v1/collections/{c}/docs/{doc_id}` | remove a document (all versions unless `?version=`) |
| DELETE | `/v1/cases/{case_id}` | purge `hosp_case_context` for a case |
| GET | `/v1/collections` | list collections with stats |
| POST | `/v1/collections/{c}/reindex` | re-embed all chunks (embedding model changed) |
| GET | `/v1/retrievals/{retrieval_id}` | fetch what was retrieved (audit) |
| GET | `/health`, `/metrics` | standard |

### 4.2 Authentication and ACL
- Header `Authorization: Bearer <service-token>`; tokens are HS256 JWTs issued by the infra bootstrap script with claims `{svc, system, role, collections[], case_scope?}`. Keycloak is not used for service-to-service calls (PROPOSED; keeps this service independent of Keycloak availability).
- `case_scope`: hospital-crew tokens include a per-call header `X-Case-Id`; for `hosp_case_context` the service injects `filter: case_id == X-Case-Id` regardless of what the caller passes.
- Violations → 403 `collection_forbidden`.

### 4.3 `POST /v1/search`

Request:
```json
{
  "collection": "ins_policy_wording",
  "query": "is robotic surgery covered under room rent cap",
  "filters": {"policy_product": "HealthPlus-A", "as_of": "2026-09-01", "chunk_type": ["text","table"]},
  "top_k": 5,
  "rerank": true,
  "metadata": {"claim_ref": "HC-2026-000123", "agent": "coverage", "prompt_version": "coverage@3"}
}
```
Response:
```json
{
  "retrieval_id": "rtr_01J9...",
  "results": [
    {
      "citation_id": "pw-HP-A-v3#p12#s4.2",
      "doc_id": "7f6b...", "text": "Room rent is limited to 1% of the sum insured per day ...",
      "score": 0.83, "dense_score": 0.79, "sparse_score": 11.2, "rerank_score": 0.83,
      "source": "HealthPlus-A wording v3", "page": 12, "section": "4.2",
      "version": 3, "chunk_type": "text"
    }
  ],
  "params": {"top_k": 5, "dense_k": 30, "sparse_k": 30, "rerank": true, "embed_model": "nomic-embed-text@768"},
  "latency_ms": {"embed": 85, "dense": 22, "sparse": 14, "fuse": 1, "rerank": 640, "total": 790}
}
```
Errors: 400 `unknown_filter`, 403 `collection_forbidden`, 409 `embed_model_mismatch`, 422 `validation_error`, 503 `embed_unavailable`.

### 4.4 `POST /v1/answer`

Request: same as search plus `question`, `answer_style` (`brief|detailed`), `max_context_tokens` (default 6000).

Response:
```json
{
  "retrieval_id": "rtr_01J9...",
  "answer": "Robotic surgery is covered as a surgical procedure, but room rent is capped at 1% of the sum insured per day [pw-HP-A-v3#p12#s4.2].",
  "citations": ["pw-HP-A-v3#p12#s4.2"],
  "insufficient_evidence": false,
  "unsupported_sentences_dropped": 0,
  "model": {"alias": "reason-cloud", "served_by": "gemini-flash", "fallback_used": false}
}
```
Rules: every sentence must end with at least one bracketed `citation_id`; the service validates ids against the retrieved set.

### 4.5 `POST /v1/ingest`

```json
{"collection": "ins_policy_wording", "bucket": "kb-sources", "key": "ins_policy_wording/healthplus-a/3/wording.pdf",
 "metadata": {"policy_product": "HealthPlus-A", "policy_version": "3", "effective_from": "2026-01-01", "effective_to": null,
              "source": "HealthPlus-A wording v3", "doc_slug": "healthplus-a-wording", "version": 3}}
→ 202 {"job_id": "job_01J9...", "status": "queued"}
```
Job status response includes `chunks_added`, `chunks_skipped`, `tables_found`, `warnings[]`.

### 4.6 `POST /v1/ingest/text` (hospital case context)

```json
{"collection": "hosp_case_context", "case_id": "c1f...", "doc_id": "d2a...", "doc_type": "discharge_summary",
 "pages": [{"page": 1, "text": "<masked text>"}], "metadata": {"source": "Discharge summary"}}
```
Idempotent by `doc_id`; re-posting replaces all chunks of that doc.

---

## 5. Build tasks

Each task: files, then verification.

1. **Scaffold** `services/rag-service/` — FastAPI app, `app/main.py`, `app/api/{search,answer,ingest,admin}.py`, `app/core/{config,auth,acl}.py`, `app/rag/{chunking,embedding,qdrant_store,sparse,fusion,rerank,citations,answer,temporal}.py`, `app/jobs/ingest_worker.py`, `app/db/models.py`, `tests/`. Dependencies: `llama-index-core`, `qdrant-client`, `fastembed` (sparse), `onnxruntime`, `tiktoken` or `transformers` tokenizer, `httpx`, `pydantic-settings`, `sqlalchemy`. Verify: `pytest -q` collects.
2. **Qdrant compose entry** in `infra/compose/shared.yml`: image `qdrant/qdrant:v1.12.x` (pin), volume `qdrant_data`, env `QDRANT__SERVICE__API_KEY`, `mem_limit 1g`, healthcheck `curl -f http://localhost:6333/readyz`. Verify: `curl -H "api-key: ..." :6333/collections`.
3. **Bootstrap script** `services/rag-service/scripts/bootstrap_collections.py`: creates all collections with config 3.2, payload indexes, writes `collection_meta` with the current embed model (read from gateway `x-embed-model`). Idempotent. Verify: re-running is a no-op.
4. **Embedding client** `app/rag/embedding.py`: batch (size 16) calls to gateway `/v1/embeddings` alias `embed` with metadata `system=shared, agent=rag-ingest|rag-query`; prefix conventions for nomic: documents `search_document: `, queries `search_query: `. Check `x-embed-model` header against `collection_meta` else raise `embed_model_mismatch`.
5. **Parsing layer** `app/rag/loaders.py`: PDFs/DOCX → doc-pipeline `POST /v1/parse` (returns markdown with headings, page numbers and tables); MD/TXT/HTML direct (`markdown-it`/`selectolax`); CSV rate tables → one chunk per logical table with markdown rendering.
6. **Chunking** `app/rag/chunking.py` (6.1).
7. **Sparse vectors** `app/rag/sparse.py`: BM25 via `fastembed` `Qdrant/bm25` model; tokenisation keeps ICD codes and alphanumeric tokens unstemmed (custom token pattern). Verify: query "I21.9" retrieves chunks containing it.
8. **Ingestion worker** `app/jobs/ingest_worker.py`: load → chunk → dedupe by `text_hash` → embed → upsert (dense+sparse in one point) → log. Single worker, batch 16 (16 GB constraint). Version handling (6.4).
9. **Hybrid retrieval** `app/rag/fusion.py`, `qdrant_store.py`: dense top 30 and sparse top 30 with identical filter → Reciprocal Rank Fusion (k=60) → top 20.
10. **Reranker** `app/rag/rerank.py`: cross-encoder `bge-reranker-base` exported to ONNX (int8), loaded lazily; skip when CPU load > 85% (psutil) or `rerank=false`. Verify: rerank improves MRR on eval set by ≥ 0.05.
11. **Temporal filter** `app/rag/temporal.py` (6.3).
12. **Citation builder** `app/rag/citations.py` (6.5) and `GET /v1/retrievals/{id}`.
13. **Answer endpoint** `app/rag/answer.py` (6.6): context packing, prompt, JSON-structured output via gateway, post-validation.
14. **ACL** `app/core/acl.py` (4.2) and `scripts/issue_service_tokens.py`.
15. **Case lifecycle** `DELETE /v1/cases/{case_id}` plus nightly janitor deleting `hosp_case_context` points where `case_closed_at < now-7d`. Event consumer for `case.closed` is hospital-n8n calling the delete endpoint.
16. **Reindex command** `POST /v1/collections/{c}/reindex` and `make rag-reindex COLLECTION=...` (6.7).
17. **Eval set** `data/eval/rag_qa.jsonl` (≥100 Q/A with gold citation ids, including 20 unanswerable and 10 temporal-trap questions) — built from the synthetic corpus of `05-integration/01`.
18. **Seed script** `make seed-kb`: uploads synthetic policies/guidelines/rules to MinIO and calls ingest for each.
19. **Metrics script** `scripts/eval_retrieval.py`: recall@k, MRR, nDCG@5, citation precision, insufficient-evidence accuracy; writes JSON + markdown to `data/eval/reports/`.
20. **Observability**: Prometheus counters/histograms (`rag_search_latency_seconds`, `rag_ingest_chunks_total`, `rag_insufficient_evidence_total`, `rag_rerank_skipped_total`), structured logs with `retrieval_id`.
21. **Tests** per section 9.

---

## 6. Key logic

### 6.1 Structure-aware chunking

Input: normalised markdown from doc-pipeline with page markers (`<!-- page:12 -->`), headings `#..####`, tables as markdown tables.

Algorithm:
```
sections = split_by_headings(doc)                       # keep heading path "4 > 4.2 Room rent limits"
for s in sections:
    blocks = split_blocks(s)                            # paragraph | list | table | definition
    for b in blocks:
        if b.type == "table":
            emit_chunk(text=b.markdown, type="table", caption=generate_caption(b))   # never split
            continue
        buffer.append(b)
        while tokens(buffer) > CHUNK_TOKENS(600):
            emit_chunk(cut_at_sentence_boundary(buffer, target=500..700))
            buffer = last OVERLAP(80) tokens of emitted chunk + remainder
    flush(buffer)
prefix every chunk text with "Heading path: <path>\n" (improves retrieval, stripped on display)
```
Rules:
- Target 400-700 tokens, overlap 80; never cut inside a table row, a numbered clause "4.2.1", or a definition entry.
- Very large table (> 1500 tokens): split by row groups, repeat header row in each chunk, `chunk_type="table"`, same `table_id`.
- Short sections (< 120 tokens) are merged with the next sibling under the same parent heading.
- Caption generation for tables: one `reason-local` call (cheap, cached) producing ≤ 25 words; stored in `caption` and prepended to the embedded text.
- `page` = page of first token; `page_end` for chunks spanning pages.
- `text_hash` = sha256 of normalised text (whitespace-collapsed, case preserved).

Pseudocode for definitions: lines matching `^"?([A-Z][\w\s-]+)"?\s+(means|shall mean)\b` are kept as a single chunk with `chunk_type="definition"` so "Pre-existing disease" style queries retrieve definitions precisely.

### 6.2 Hybrid retrieval

```python
def search(req, caller):
    acl.assert_allowed(caller, req.collection, req.filters)
    flt = build_filter(req.filters, caller)  # system, policy_product, temporal, case_id ...
    q_dense = embed(f"search_query: {req.query}")
    q_sparse = bm25_query(req.query)  # keep codes unstemmed
    dense = qdrant.query(vector=("dense", q_dense), filter=flt, limit=30)
    sparse = qdrant.query(vector=("bm25", q_sparse), filter=flt, limit=30)
    fused = rrf([dense, sparse], k=60)[:20]
    ranked = rerank(req.query, fused) if should_rerank(req) else fused
    final = dedupe_adjacent(ranked)[: min(req.top_k, MAX_TOP_K)]
    rid = log_retrieval(caller, req, final)
    return build_response(rid, final)
```
`dedupe_adjacent`: if two chunks from the same section are consecutive (`chunk_index` differ by 1) and both in top-k, keep both but mark `adjacent_to` so the answer prompt can merge them.

Score normalisation: final `score` is reranker sigmoid output if reranked, else RRF score min-max normalised to [0,1] among candidates (documented, because thresholds depend on it: `MIN_SCORE` applies to the reranked score; when rerank is skipped use `MIN_SCORE_NO_RERANK=0.02` on the raw RRF score, PROPOSED, calibrate on the eval set).

### 6.3 Temporal filtering (policy versions as of the claim date)

- Callers pass `as_of` (claim admission date). Filter: `effective_from <= as_of AND (effective_to IS NULL OR effective_to > as_of)`.
- If `policy_product` given and multiple versions overlap (data error), prefer the highest `version`; log warning.
- If no chunk satisfies the temporal filter but older/newer exist, the response includes `diagnostics.temporal_miss=true` and the nearest version label; agents must not use out-of-date text for a decision but may show it to the human reviewer as "other version".
- Ingestion sets `effective_to` on previous versions automatically when a new version with later `effective_from` is ingested (only if admin passes `supersede=true`).

### 6.4 Idempotent ingestion and versioning

- Point id = UUIDv5(collection|doc_id|version|chunk_index). Unchanged chunks (same `text_hash`) are skipped (`chunks_skipped`).
- Re-ingesting same `doc_id+version` with different content → replace: delete points of that `doc_id+version` not present in new chunk set.
- New `version` → new points; older versions retained (needed for historical claims).
- Document removal (`DELETE ... /docs/{doc_id}`) removes points and logs an admin audit event.
- Ingest requests are accepted only for the caller's allowed collections.

### 6.5 Citation identifiers

Format: `{doc_slug_short}#p{page}#s{section}` e.g. `pw-HP-A-v3#p12#s4.2`; when the section is unknown use `c{chunk_index}`. Short slug stored in payload `citation_prefix`. IDs are stable across re-ingestion of unchanged content. For hospital case context: `case-{short}#doc-{doc_type}#p{page}`.

The UI (hospital and insurer review screens) renders a citation as a link to the source document page at the exact page via `source_url` (presigned MinIO URL minted by the owning API, not by rag-service).

### 6.6 Grounded answer procedure

```
retrieve(question, filters, top_k=8, rerank=true)
if no results or max(score) < MIN_SCORE(0.35):  return {insufficient_evidence: true, answer: null}
context = pack(results, max_context_tokens=6000, order=by score, merge adjacent)
prompt = SYSTEM: "Answer ONLY from the provided passages. Every sentence must end with [citation_id].
                  If the passages do not answer, set insufficient_evidence=true. Do not use outside knowledge."
         + passages with ids + question
resp = gateway.chat(alias=reason-cloud, response_format=json_schema{answer, citations[], insufficient_evidence}, temperature=0)
validate:
   - every id in resp.citations ∈ retrieved ids          else drop sentence / mark insufficient
   - every sentence has ≥ 1 valid [id]                   else drop sentence (count unsupported_sentences_dropped)
   - answer non-empty after dropping                     else insufficient_evidence=true
   - numeric claims (₹ amounts, %, days): each number must appear in a cited passage (regex check)   else drop sentence
return result with retrieval_id
```
The numeric check matters for coverage text ("1% of sum insured") because wrong numbers are the costliest hallucination.

Hospital query drafts: `hosp_case_context` search with `case_id` filter first; passages from the case's documents are quoted with page citations; if the query asks for an item not in any document (e.g. missing implant sticker) the answer states it is missing instead of inventing (supports "fixable before fatal").

### 6.7 Embedding-model change and reindex

- `collection_meta.embed_model` must equal the gateway's reported model at ingest and search time, else 409 `embed_model_mismatch`.
- `reindex`: for each point, re-embed `text` (batches of 16), write to a **new** collection `<name>__reindex_<ts>` with the new dimension, verify counts, then swap alias (Qdrant collection aliases) and drop the old one after a configurable delay. Progress via job endpoint. Estimated cost documented: ~1 s per 16 chunks locally.

### 6.8 Ephemeral case context

- `hosp_case_context` payload includes `case_id`, `doc_id`, `pii_masked=true`.
- Ingestion refuses text failing a cheap PII regex check (same patterns as gateway guard), returning 422 `pii_detected` to protect the principle that raw IDs never leave doc-pipeline's masked output.
- Deletion: `DELETE /v1/cases/{case_id}` on `case.closed`; janitor removes leftovers after 7 days; deletion count reported in audit via hospital-api.

---

## 7. Configuration

```
RAG_PORT=8400
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=
LLM_GATEWAY_URL=http://llm-gateway:4000
LLM_GATEWAY_KEY=               # virtual key rag-service
DOCPIPE_URL=http://doc-pipeline:8200
MINIO_ENDPOINT=minio:9000      MINIO_ACCESS_KEY=   MINIO_SECRET_KEY=   KB_BUCKET=kb-sources
EMBED_ALIAS=embed              EMBED_BATCH=16
CHUNK_TOKENS=600               CHUNK_OVERLAP=80      CHUNK_MIN_TOKENS=120
DENSE_K=30                     SPARSE_K=30           FUSE_K=60          RERANK_INPUT=20
RERANK=on                      RERANK_MODEL=bge-reranker-base-onnx-int8   RERANK_CPU_CEILING=85
MIN_SCORE=0.35                 MIN_SCORE_NO_RERANK=0.02
MAX_TOP_K=20                   MAX_CONTEXT_TOKENS=6000
CASE_CONTEXT_TTL_DAYS=7
JWT_SECRET=                    SERVICE_TOKENS_FILE=/run/secrets/rag_tokens.json
DB_URL=sqlite:///data/rag.db   # or Postgres schema in insurer-db? NO: keep independent, own SQLite volume
LOG_LEVEL=info
```
Memory budget: rag-service ≤ 1.5 GB (ONNX reranker ≈ 300 MB int8, fastembed BM25 small), Qdrant ≤ 1 GB.

---

## 8. Edge cases and failure handling

| # | Situation | Behaviour |
|---|---|---|
| 1 | Embedding model swapped | `embed_model_mismatch` 409; run `reindex`; search blocked until done (or served from old collection until alias swap). |
| 2 | Duplicate documents / re-uploaded same version | Skipped by `text_hash`; report counts. |
| 3 | New policy version with overlapping dates | Admin must pass `supersede=true` or ingestion fails with 409 `version_overlap`. |
| 4 | Tables (sub-limit schedules) split badly | Tables are atomic chunks (or header-repeated row groups) with a caption; test T6 checks no table row is cut. |
| 5 | Queries containing ICD / procedure codes | Sparse BM25 with unstemmed alphanumeric tokens; query analyser detects code patterns and boosts sparse weight in RRF (weights dense 1.0, sparse 1.5 when codes present). |
| 6 | Scanned policy PDFs with OCR errors | doc-pipeline confidence < threshold → chunks flagged `low_ocr_confidence=true`; search ranks them lower (×0.9) and the answer endpoint adds a warning; admin sees an ingestion warning. |
| 7 | Cross-system leakage attempts | ACL denies; test that hospital token cannot query `ins_*` or pass `collection` aliases; Qdrant API key also separated per system (two keys, read vs read-write) — PROPOSED optional hardening using Qdrant JWT/RBAC if available. |
| 8 | Ephemeral case data after closure | Deleted on `case.closed`; if delete fails the janitor retries; metric alerts when residual points > 0 for closed cases. |
| 9 | Very long documents (500 pages) | Ingestion streams page batches; memory bounded; job progress reported; chunk cap per doc 20k with warning. |
| 10 | Reranker unavailable / CPU overloaded | Skipped; response `params.rerank=false`; thresholds switch to `MIN_SCORE_NO_RERANK`. |
| 11 | Gateway embed down | Search returns 503 `embed_unavailable`; ingestion jobs retry with backoff 3×, then fail with message; **no fallback to a different-dimension model**. |
| 12 | LLM answer cites an id not retrieved | Sentence dropped; if all dropped → `insufficient_evidence`. |
| 13 | Numbers in answer not in cited passage | Sentence dropped (numeric check). |
| 14 | Contradictory passages (two versions) | Temporal filter prevents; if both valid (e.g. rider overriding base), answer must cite both and flag `conflict=true` (PROPOSED; prompt instructs to state both). |
| 15 | Empty query / only stop words | 422. |
| 16 | `top_k` > 20 | Clamped to `MAX_TOP_K` with warning in response. |
| 17 | Concurrent ingest of same doc | Advisory lock keyed by `doc_id`; second job waits or returns 409 `ingest_in_progress`. |
| 18 | Qdrant restart during ingest | Job marks failed; re-run is idempotent. |
| 19 | PII in case context submission | 422 `pii_detected`; hospital-crew must send the masked text. |
| 20 | Prompt injection inside policy docs or case documents ("ignore previous instructions") | Passages are delimited and the system prompt states they are untrusted data; output is schema-validated; answer endpoint never executes tools. Test with injected corpus (section 9). |
| 21 | Language: Hindi/regional text in case documents | Embedding model English-centric; flagged `language != en` and retrieval quality warning; out of scope for v1 beyond graceful handling. |

---

## 9. Tests

### 9.1 Test matrix

| ID | Area | Test | Expected |
|---|---|---|---|
| T1 | Chunking | heading path preserved; no chunk > 750 tokens (non-table) | pass |
| T2 | Chunking | overlap 80 tokens between consecutive chunks of same section | pass |
| T3 | Chunking | numbered clauses "4.2.1" never split mid-clause | pass |
| T4 | Chunking | definitions emitted as single chunks | pass |
| T5 | Chunking | tables atomic; oversized table split by rows with header repeated | pass |
| T6 | Chunking | table caption present and ≤ 25 words | pass |
| T7 | Ingest | idempotent: re-ingest same doc → `chunks_added=0` | pass |
| T8 | Ingest | changed text in one paragraph → only affected chunks replaced | pass |
| T9 | Ingest | new version → both versions queryable by `as_of` | pass |
| T10 | Ingest | `supersede=true` sets `effective_to` on old version | pass |
| T11 | Retrieval | ICD code query finds exact-code chunk in top 3 via sparse | pass |
| T12 | Retrieval | semantic paraphrase finds clause via dense | pass |
| T13 | Retrieval | hybrid ≥ dense-only and sparse-only on recall@5 (eval set) | pass |
| T14 | Retrieval | rerank improves MRR by ≥ 0.05 | pass |
| T15 | Retrieval | rerank skipped under CPU ceiling; thresholds adapt | pass |
| T16 | Temporal | `as_of` before first version → empty + `temporal_miss` | pass |
| T17 | Temporal | `as_of` inside window returns right version | pass |
| T18 | ACL | hospital token cannot read `ins_*` (403) | pass |
| T19 | ACL | hospital token reads only own `case_id` | pass |
| T20 | ACL | insurer token cannot write | pass |
| T21 | Answer | unanswerable questions → `insufficient_evidence` ≥ 90% | pass |
| T22 | Answer | every sentence has valid citation; invalid ids dropped | pass |
| T23 | Answer | numeric check drops sentence with number absent from passage | pass |
| T24 | Answer | prompt-injection in passage does not alter behaviour | pass |
| T25 | Answer | conflicting versions → both cited | pass |
| T26 | Lifecycle | `DELETE /v1/cases/{id}` removes all points; subsequent search empty | pass |
| T27 | Lifecycle | janitor deletes 7-day-old closed-case data | pass |
| T28 | Reindex | reindex to different dim produces working collection; old alias dropped | pass |
| T29 | Embed | model mismatch → 409 | pass |
| T30 | PII | `/v1/ingest/text` with Aadhaar → 422 | pass |
| T31 | Perf | search p95 < 1.5 s with rerank, < 400 ms without (10k chunks) | pass |
| T32 | Perf | ingest 200-page PDF < 6 min on dev laptop; memory < 1.5 GB | pass |
| T33 | Audit | `retrieval_id` fetchable and lists passages used | pass |

### 9.2 Retrieval quality targets (on `rag_qa.jsonl`)

| Metric | Target |
|---|---|
| recall@5 (gold citation in top 5) | ≥ 0.85 |
| MRR | ≥ 0.6 |
| nDCG@5 | ≥ 0.7 |
| citation precision (cited ids that are gold-relevant) | ≥ 0.9 |
| insufficient-evidence accuracy on unanswerable set | ≥ 0.9 |
| temporal-trap accuracy (right version) | ≥ 0.95 |
| numeric-faithfulness (numbers present in cited passages) | 1.0 |

### 9.3 Eval set composition (100+ rows)

`data/eval/rag_qa.jsonl` row:
```json
{"id":"q-017","collection":"ins_policy_wording","question":"What is the ICU daily limit for sum insured 5 lakh?",
 "filters":{"policy_product":"HealthPlus-A","as_of":"2026-03-01"},
 "gold_citations":["pw-HP-A-v3#p12#s4.3"],"answerable":true,"type":"table_lookup"}
```
Types: `definition` (15), `exclusion` (20), `table_lookup` (20), `paraphrase` (15), `code_lookup` (10), `temporal_trap` (10), `unanswerable` (20), `multi_hop` (5), `injection` (5).

---

## 10. Acceptance criteria

- [ ] All metrics in 9.2 met on the synthetic corpus; report committed under `data/eval/reports/`.
- [ ] Hospital credentials cannot read any insurer collection (T18 green).
- [ ] Each answer sentence traceable to a valid citation; numeric check active.
- [ ] Reindex path works end to end (T28).
- [ ] `hosp_case_context` purge on `case.closed` verified with hospital-n8n flow (integration test with Dev A).
- [ ] Memory: rag-service + Qdrant ≤ 2.5 GB under the seeded corpus.
- [ ] `make seed-kb` reproducibly builds the full KB from synthetic data.
- [ ] Prometheus metrics and `retrieval_id` audit fetch available.

---

## 11. Dependencies

| Depends on | For |
|---|---|
| llm-gateway (04-04): `embed`, `reason-cloud`, `reason-local` | embeddings, answers, table captions |
| doc-pipeline (04-02) `/v1/parse` | PDF/DOCX structure |
| MinIO `kb-sources` bucket (04-01) | source storage |
| Synthetic corpus (05-integration/01) | policies, guidelines, rules, case history |
| Config versioning (01-03) | policy version/effective dates alignment with `policy_rules` rows |
| hospital-n8n `case.closed` hook (02-dev-A/08) | ephemeral data deletion |

Consumed by: insurer-crew Coverage/Query-draft agents (03/08), hospital-crew Query Responder (02/09), eval harness (05/02).

Interface freeze (needs both developers): collection names, `citation_id` format, response shapes in 4.3/4.4, `retrieval_id` usage in audit events.

---

## 12. Claude Code kickoff prompt

> Implement docs/implementation/04-shared-services/05-rag-service-qdrant.md tasks 1-21. Order: scaffold, Qdrant compose + bootstrap, embedding client, chunking, ingestion, hybrid search with eval set, then reranker, ACL, answer endpoint, lifecycle/reindex. Write the chunking and temporal-filter unit tests before the code. Run `scripts/eval_retrieval.py` and report recall@5, MRR, citation precision and insufficient-evidence accuracy before finishing. Do not call any provider directly; all LLM and embedding calls go through llm-gateway. Mark any new decisions PROPOSED in the doc.
