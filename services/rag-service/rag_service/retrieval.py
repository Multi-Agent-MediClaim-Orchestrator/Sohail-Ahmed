"""Hybrid retrieval: filters (incl. temporal window), RRF fusion, optional rerank, citation ids (04-05 §6.2, §6.3, §6.5)."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from .store import Filter, Hit, Store
from .text import has_code, sparse_vector, tokenize

# -------------------------------------------------------------------------------------------
# fusion
# -------------------------------------------------------------------------------------------


def rrf(rankings: Sequence[tuple[list[Hit], float]], k: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion with per-ranking weights."""
    score: dict[str, float] = {}
    keep: dict[str, Hit] = {}
    for hits, weight in rankings:
        for rank, h in enumerate(hits, start=1):
            score[h.id] = score.get(h.id, 0.0) + weight / (k + rank)
            keep.setdefault(h.id, h)
    fused = [Hit(i, s, keep[i].payload) for i, s in score.items()]
    fused.sort(key=lambda h: (-h.score, h.id))
    return fused


def minmax(hits: list[Hit]) -> list[Hit]:
    if not hits:
        return hits
    hi, lo = max(h.score for h in hits), min(h.score for h in hits)
    span = hi - lo
    return [Hit(h.id, 1.0 if span == 0 else (h.score - lo) / span, h.payload) for h in hits]


# -------------------------------------------------------------------------------------------
# rerank
# -------------------------------------------------------------------------------------------
class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class LexicalReranker:
    """Dependency-free reranker: query-term coverage, exact phrase and number match, length prior. A cross-encoder
    (bge-reranker-base int8 ONNX) implements the same Protocol and is selected with ``RERANK_MODEL`` (not bundled)."""

    name = "lexical-v1"

    def score(self, query: str, passages: list[str]) -> list[float]:
        q = tokenize(query)
        qset = set(q)
        nums = set(re.findall(r"\d+(?:\.\d+)?", query))
        out = []
        for p in passages:
            toks = tokenize(p)
            tset = set(toks)
            if not qset:
                out.append(0.0)
                continue
            cover = len(qset & tset) / len(qset)
            bigrams = {f"{a} {b}" for a, b in zip(q, q[1:], strict=False)}
            ptext = " ".join(toks)
            phrase = sum(1 for b in bigrams if b in ptext) / max(len(bigrams), 1)
            num = (sum(1 for n in nums if n in p) / len(nums)) if nums else 0.0
            raw = 2.2 * cover + 1.8 * phrase + 1.0 * num - 0.2 * math.log1p(len(toks) / 200)
            out.append(1 / (1 + math.exp(-(raw - 1.6))))
        return out


def cpu_busy(ceiling: int) -> bool:
    try:
        import psutil

        return psutil.cpu_percent(interval=None) > ceiling
    except Exception:  # noqa: BLE001
        return False


# -------------------------------------------------------------------------------------------
# citations
# -------------------------------------------------------------------------------------------
def citation_id(payload: dict[str, Any]) -> str:
    if payload.get("citation_id"):
        return str(payload["citation_id"])
    prefix = payload.get("citation_prefix") or payload.get("doc_slug") or "doc"
    if payload.get("system") == "hospital" and payload.get("case_id"):
        return f"case-{str(payload['case_id'])[:8]}#doc-{payload.get('doc_type', 'doc')}#p{payload.get('page', 1)}"
    sect = payload.get("section")
    return f"{prefix}#p{payload.get('page', 1)}#" + (f"s{sect}" if sect else f"c{payload.get('chunk_index', 0)}")


# -------------------------------------------------------------------------------------------
# search
# -------------------------------------------------------------------------------------------
@dataclass
class SearchParams:
    top_k: int = 5
    dense_k: int = 30
    sparse_k: int = 30
    fuse_k: int = 60
    rerank_input: int = 20
    rerank: bool = True
    cpu_ceiling: int = 85
    max_top_k: int = 20
    min_score_no_rerank: float = 0.02


@dataclass
class SearchResult:
    results: list[dict[str, Any]]
    params: dict[str, Any]
    latency_ms: dict[str, int]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reranked: bool = False


def build_filter(filters: dict[str, Any], forced: dict[str, list[Any]] | None = None) -> Filter:
    """Caller filters -> Filter. Unknown keys raise ValueError (400 ``unknown_filter``)."""
    allowed = {"policy_product", "policy_version", "as_of", "chunk_type", "doc_type", "doc_id", "version", "system", "language"}
    unknown = set(filters) - allowed
    if unknown:
        raise ValueError(f"unknown_filter: {sorted(unknown)}")
    match: dict[str, list[Any]] = {}
    as_of: date | None = None
    for k, v in filters.items():
        if k == "as_of":
            as_of = v if isinstance(v, date) else date.fromisoformat(str(v))
        elif v is not None:
            match[k] = v if isinstance(v, list) else [v]
    for k, v in (forced or {}).items():  # ACL-injected conditions always win
        match[k] = v
    return Filter(match=match, as_of=as_of)


def search(store: Store, embedder: Any, collection: str, query: str, filters: dict[str, Any], params: SearchParams, reranker: Reranker | None = None,
           forced: dict[str, list[Any]] | None = None) -> SearchResult:
    t0 = time.perf_counter()
    if not tokenize(query):
        raise ValueError("empty_query")
    flt = build_filter(filters, forced)
    top_k = min(params.top_k, params.max_top_k)
    warnings = [f"top_k clamped to {params.max_top_k}"] if params.top_k > params.max_top_k else []

    t = time.perf_counter()
    qv = embedder.embed([f"search_query: {query}"], kind="query")[0]
    t_embed = time.perf_counter() - t
    t = time.perf_counter()
    dense = store.query_dense(collection, qv, flt, params.dense_k)
    t_dense = time.perf_counter() - t
    t = time.perf_counter()
    sparse = store.query_sparse(collection, sparse_vector(query, query=True), flt, params.sparse_k)
    t_sparse = time.perf_counter() - t

    codes = has_code(query)  # exact-code queries lean on the sparse ranking (04-05 §8 #5)
    fused = rrf([(dense, 1.0), (sparse, 1.5 if codes else 1.0)], params.fuse_k)[: params.rerank_input]
    dense_s = {h.id: h.score for h in dense}
    sparse_s = {h.id: h.score for h in sparse}

    diag: dict[str, Any] = {}
    if not fused and flt.as_of is not None:  # temporal miss: do older/newer versions exist for the same filter?
        relaxed = Filter(match=flt.match, as_of=None)
        other = store.query_sparse(collection, sparse_vector(query, query=True), relaxed, 1) or store.query_dense(collection, qv, relaxed, 1)
        if other:
            diag = {"temporal_miss": True, "nearest_version": other[0].payload.get("version"), "nearest_effective_from": other[0].payload.get("effective_from")}

    do_rerank = params.rerank and reranker is not None and bool(fused) and not cpu_busy(params.cpu_ceiling)
    t = time.perf_counter()
    if do_rerank:
        scores = reranker.score(query, [h.payload.get("text", "") for h in fused])  # type: ignore[union-attr]
        # low-confidence OCR chunks rank lower (x0.9)
        final_hits = [Hit(h.id, s * (0.9 if h.payload.get("low_ocr_confidence") else 1.0), h.payload) for h, s in zip(fused, scores, strict=True)]
        final_hits.sort(key=lambda h: (-h.score, h.id))
        rr_scores = {h.id: h.score for h in final_hits}
    else:
        final_hits = fused
        rr_scores = {}
        if params.rerank and fused:
            warnings.append("rerank skipped")
    t_rerank = time.perf_counter() - t

    final_hits = final_hits[:top_k]
    results = []
    for h in final_hits:
        p = h.payload
        results.append({
            "citation_id": citation_id(p), "id": h.id, "doc_id": p.get("doc_id"), "text": p.get("text", ""), "score": round(h.score, 6),
            "dense_score": round(dense_s.get(h.id, 0.0), 6), "sparse_score": round(sparse_s.get(h.id, 0.0), 6),
            **({"rerank_score": round(rr_scores[h.id], 6)} if h.id in rr_scores else {}),
            "source": p.get("source"), "page": p.get("page"), "section": p.get("section"), "version": p.get("version"), "chunk_type": p.get("chunk_type"),
            "chunk_index": p.get("chunk_index"), "policy_product": p.get("policy_product"), "effective_from": p.get("effective_from"), "effective_to": p.get("effective_to"), "low_ocr_confidence": bool(p.get("low_ocr_confidence", False)),
        })
    # mark adjacent chunks of the same doc so the answer prompt can merge them
    by_doc: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for r in results:
        by_doc.setdefault((r["doc_id"], r["version"]), []).append(r)
    for rs in by_doc.values():
        idx = {r["chunk_index"]: r["citation_id"] for r in rs}
        for r in rs:
            adj = [c for i, c in idx.items() if r["chunk_index"] is not None and i is not None and abs(i - r["chunk_index"]) == 1]
            if adj:
                r["adjacent_to"] = adj
    total = time.perf_counter() - t0
    return SearchResult(
        results=results, reranked=do_rerank, diagnostics=diag, warnings=warnings,
        params={"top_k": top_k, "dense_k": params.dense_k, "sparse_k": params.sparse_k, "rerank": do_rerank, "embed_model": getattr(embedder, "model_id", "?"), "codes_boost": codes,
                "min_score_no_rerank": params.min_score_no_rerank},
        latency_ms={"embed": int(t_embed * 1000), "dense": int(t_dense * 1000), "sparse": int(t_sparse * 1000), "rerank": int(t_rerank * 1000), "total": int(total * 1000)},
    )
