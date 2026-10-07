"""rag-service FastAPI app (04-05 §4)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from . import ingest as ing
from . import security
from .answer import answer as make_answer
from .config import Settings, get_settings
from .db import Meta
from .retrieval import LexicalReranker, SearchParams, search
from .security import AccessError, Caller
from .store import Filter, MemoryStore, Store
from .text import HashEmbedder, mask_pii

log = logging.getLogger("rag")

COLLECTIONS = ["ins_policy_wording", "ins_medical_guidelines", "ins_case_history", "hosp_insurer_rules", "hosp_case_context"]


class SearchReq(BaseModel):
    collection: str
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = 5
    rerank: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerReq(SearchReq):
    question: str
    answer_style: str = "brief"
    max_context_tokens: int | None = None
    top_k: int = 8


class IngestReq(BaseModel):
    collection: str
    bucket: str
    key: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    supersede: bool = False


class IngestTextReq(BaseModel):
    collection: str = "hosp_case_context"
    case_id: str
    doc_id: str
    doc_type: str
    pages: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(settings: Settings | None = None, *, store: Store | None = None, meta: Meta | None = None, embedder: Any = None, chat: Callable[..., dict[str, Any]] | None = None,
               reranker: Any = None, loader: Callable[[str, str], bytes] | None = None, docpipe: Callable[[str, bytes], str] | None = None, background_inline: bool = False) -> FastAPI:
    cfg = settings or get_settings()
    store = store or MemoryStore()
    meta = meta or Meta(":memory:")
    embedder = embedder or HashEmbedder(cfg.embed_dim)
    reranker = reranker if reranker is not None else (LexicalReranker() if cfg.rerank == "on" else None)
    app = FastAPI(title="rag-service", version="1.0")

    reg = CollectorRegistry()
    m_search = Histogram("rag_search_latency_seconds", "search latency", registry=reg, buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))
    m_chunks = Counter("rag_ingest_chunks_total", "chunks ingested", ["collection", "result"], registry=reg)
    m_insuf = Counter("rag_insufficient_evidence_total", "answers returned as insufficient_evidence", registry=reg)
    m_skip = Counter("rag_rerank_skipped_total", "searches where rerank was skipped", registry=reg)
    m_resid = Gauge("rag_case_context_points", "points in hosp_case_context", registry=reg)

    app.state.store, app.state.meta, app.state.embedder = store, meta, embedder

    @app.exception_handler(AccessError)
    async def _access(_: Request, e: AccessError) -> JSONResponse:
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)

    @app.exception_handler(ing.IngestError)
    async def _ing(_: Request, e: ing.IngestError) -> JSONResponse:
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)

    def caller(authorization: str | None = Header(default=None)) -> Caller:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AccessError(401, "invalid_token", "missing bearer token")
        return security.decode_token(cfg.jwt_secret, authorization.split(None, 1)[1])

    def _read(c: Caller, collection: str) -> None:
        if collection not in COLLECTIONS and not collection.startswith("eval_"):
            raise AccessError(404, "unknown_collection", collection)
        security.check(c, collection)

    def _write(c: Caller, collection: str) -> None:
        if collection not in COLLECTIONS and not collection.startswith("eval_"):
            raise AccessError(404, "unknown_collection", collection)
        security.check(c, collection, write=True)

    def _embed_ok(collection: str) -> None:
        rec = meta.get_collection(collection)
        if rec and rec["embed_model"] != embedder.model_id:
            raise ing.IngestError(409, "embed_model_mismatch", f"collection built with {rec['embed_model']}, gateway reports {embedder.model_id}")

    def _do_search(c: Caller, req: SearchReq, case_id: str | None, query: str, top_k: int) -> tuple[Any, str]:
        _read(c, req.collection)
        if not store.collection_exists(req.collection):
            raise AccessError(404, "collection_empty", f"{req.collection} has not been created yet")
        _embed_ok(req.collection)
        forced = security.forced_filter(c, req.collection, case_id)
        params = SearchParams(top_k=top_k, dense_k=cfg.dense_k, sparse_k=cfg.sparse_k, fuse_k=cfg.fuse_k, rerank_input=cfg.rerank_input, rerank=req.rerank,
                              cpu_ceiling=cfg.rerank_cpu_ceiling, max_top_k=cfg.max_top_k, min_score_no_rerank=cfg.min_score_no_rerank)
        try:
            with m_search.time():
                res = search(store, embedder, req.collection, query, req.filters, params, reranker, forced)
        except ValueError as e:
            msg = str(e)
            code = "unknown_filter" if msg.startswith("unknown_filter") else "validation_error"
            raise AccessError(400 if code == "unknown_filter" else 422, code, msg) from e
        if req.rerank and not res.reranked and res.results:
            m_skip.inc()
        rid = meta.log_retrieval(c.svc, req.collection, query, mask_pii(query), [r["citation_id"] for r in res.results], [r["score"] for r in res.results])
        return res, rid

    # ------------------------------------------------------------------ search / answer
    @app.post("/v1/search")
    def search_ep(req: SearchReq, c: Caller = Depends(caller), x_case_id: str | None = Header(default=None)) -> dict[str, Any]:
        res, rid = _do_search(c, req, x_case_id, req.query, req.top_k)
        out: dict[str, Any] = {"retrieval_id": rid, "results": res.results, "params": res.params, "latency_ms": res.latency_ms}
        if res.diagnostics:
            out["diagnostics"] = res.diagnostics
        if res.warnings:
            out["warnings"] = res.warnings
        return out

    @app.post("/v1/answer")
    def answer_ep(req: AnswerReq, c: Caller = Depends(caller), x_case_id: str | None = Header(default=None)) -> dict[str, Any]:
        if chat is None:
            raise AccessError(503, "llm_unavailable", "no gateway chat client configured")
        res, rid = _do_search(c, req, x_case_id, req.question, req.top_k)
        out = make_answer(req.question, res.results, reranked=res.reranked, min_score=cfg.min_score, min_score_no_rerank=cfg.min_score_no_rerank, chat=chat,
                          max_context_tokens=req.max_context_tokens or cfg.max_context_tokens, style=req.answer_style)
        if out["insufficient_evidence"]:
            m_insuf.inc()
        if res.diagnostics:
            out["diagnostics"] = res.diagnostics
        return {"retrieval_id": rid, **out}

    # ------------------------------------------------------------------ ingestion
    def _run_ingest(jid: str, req: IngestReq) -> None:
        meta.update_job(jid, status="running")
        doc_id = ing.doc_id_for(req.collection, req.metadata.get("doc_slug") or req.key)
        if not meta.try_lock(doc_id, jid):
            meta.update_job(jid, status="failed", error="ingest_in_progress", finished_at=ing.now_iso())
            return
        try:
            if loader is None:
                raise ing.IngestError(501, "storage_unavailable", "no object-store loader configured")
            md = ing.load_markdown(req.key.rsplit("/", 1)[-1], loader(req.bucket, req.key), docpipe)
            doc = {**req.metadata, "system": req.metadata.get("system") or ("insurer" if req.collection.startswith("ins_") else "hospital")}
            rep = ing.ingest_markdown(store, meta, embedder, req.collection, md, doc, supersede=req.supersede, chunk_tokens=cfg.chunk_tokens, overlap=cfg.chunk_overlap,
                                      min_tokens=cfg.chunk_min_tokens, max_tokens=cfg.chunk_max_tokens, embed_batch=cfg.embed_batch)
            m_chunks.labels(req.collection, "added").inc(rep.chunks_added)
            m_chunks.labels(req.collection, "skipped").inc(rep.chunks_skipped)
            meta.update_job(jid, status="done", chunks_added=rep.chunks_added, chunks_skipped=rep.chunks_skipped, tables_found=rep.tables_found, warnings=rep.warnings, finished_at=ing.now_iso())
        except ing.IngestError as e:
            meta.update_job(jid, status="failed", error=f"{e.code}: {e.message}", finished_at=ing.now_iso())
        except Exception as e:  # noqa: BLE001
            log.exception("ingest failed")
            meta.update_job(jid, status="failed", error=f"{type(e).__name__}: {e}", finished_at=ing.now_iso())
        finally:
            meta.unlock(doc_id)

    @app.post("/v1/ingest", status_code=202)
    def ingest_ep(req: IngestReq, bg: BackgroundTasks, c: Caller = Depends(caller)) -> dict[str, str]:
        _write(c, req.collection)
        jid = meta.new_job(req.collection, req.bucket, req.key)
        (_run_ingest(jid, req) if background_inline else bg.add_task(_run_ingest, jid, req))
        return {"job_id": jid, "status": "queued"}

    @app.get("/v1/ingest/{job_id}")
    def job_ep(job_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        j = meta.get_job(job_id)
        if j is None:
            raise AccessError(404, "not_found", "unknown job")
        _read(c, j["collection"])
        return j

    @app.post("/v1/ingest/text")
    def ingest_text_ep(req: IngestTextReq, c: Caller = Depends(caller), x_case_id: str | None = Header(default=None)) -> dict[str, Any]:
        if req.collection != "hosp_case_context":
            raise AccessError(422, "validation_error", "ingest/text is only for hosp_case_context")
        _write(c, req.collection)
        if c.svc == "hospital-crew" and x_case_id != req.case_id:
            raise AccessError(403, "collection_forbidden", "X-Case-Id must equal case_id")
        rep = ing.ingest_case_text(store, meta, embedder, req.case_id, req.doc_id, req.doc_type, req.pages, source=req.metadata.get("source", ""),
                                   chunk_tokens=cfg.chunk_tokens, overlap=cfg.chunk_overlap, min_tokens=cfg.chunk_min_tokens, max_tokens=cfg.chunk_max_tokens, embed_batch=cfg.embed_batch)
        m_chunks.labels(req.collection, "added").inc(rep.chunks_added)
        return {"doc_id": rep.doc_id, "chunks_added": rep.chunks_added, "chunks_skipped": rep.chunks_skipped, "chunks_deleted": rep.chunks_deleted}

    # ------------------------------------------------------------------ admin / lifecycle
    @app.delete("/v1/collections/{collection}/docs/{doc_id}")
    def delete_doc(collection: str, doc_id: str, version: int | None = Query(default=None), c: Caller = Depends(caller)) -> dict[str, int]:
        _write(c, collection)
        match: dict[str, list[Any]] = {"doc_id": [doc_id]}
        if version is not None:
            match["version"] = [version]
        n = store.delete_where(collection, Filter(match=match))
        log.info("admin audit: doc deleted collection=%s doc_id=%s version=%s by=%s points=%d", collection, doc_id, version, c.svc, n)
        return {"deleted": n}

    @app.delete("/v1/cases/{case_id}")
    def delete_case(case_id: str, c: Caller = Depends(caller), x_case_id: str | None = Header(default=None)) -> dict[str, int]:
        _write(c, "hosp_case_context")
        if c.svc == "hospital-crew" and x_case_id != case_id:
            raise AccessError(403, "collection_forbidden", "X-Case-Id must equal case_id")
        n = ing.purge_case(store, case_id) if store.collection_exists("hosp_case_context") else 0
        return {"deleted": n}

    @app.post("/v1/cases/{case_id}/close")
    def close_case(case_id: str, c: Caller = Depends(caller)) -> dict[str, int]:
        _write(c, "hosp_case_context")
        return {"marked": ing.mark_case_closed(store, case_id) if store.collection_exists("hosp_case_context") else 0}

    @app.post("/v1/admin/janitor")
    def janitor_ep(c: Caller = Depends(caller), today: date | None = None) -> dict[str, int]:
        _write(c, "hosp_case_context")
        return {"deleted": ing.janitor(store, cfg.case_context_ttl_days, today) if store.collection_exists("hosp_case_context") else 0}

    @app.get("/v1/collections")
    def collections_ep(c: Caller = Depends(caller)) -> list[dict[str, Any]]:
        out = []
        for name, st in store.stats().items():
            try:
                security.check(c, name)
            except AccessError:
                continue
            out.append({"collection": name, **st, **(meta.get_collection(name) or {})})
        return out

    @app.post("/v1/collections/{collection}/reindex")
    def reindex_ep(collection: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        _write(c, collection)
        if c.svc.endswith("-crew"):
            raise AccessError(403, "collection_forbidden", "reindex is an admin operation")
        return ing.reindex(store, meta, embedder, collection, embed_batch=cfg.embed_batch)

    @app.get("/v1/retrievals/{rid}")
    def retrieval_ep(rid: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        r = meta.get_retrieval(rid)
        if r is None:
            raise AccessError(404, "not_found", "unknown retrieval")
        _read(c, r["collection"])
        return r

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "embed_model": embedder.model_id, "rerank": reranker.name if reranker else None}

    @app.get("/metrics")
    def metrics() -> Response:
        if store.collection_exists("hosp_case_context"):
            m_resid.set(store.count("hosp_case_context"))
        return Response(generate_latest(reg), media_type="text/plain; version=0.0.4")

    return app
