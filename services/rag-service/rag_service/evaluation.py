"""Retrieval/answer evaluation against the synthetic KB (04-05 §9.2). Pure functions; ``scripts/eval_retrieval.py`` is the CLI."""

from __future__ import annotations

import math
import re
from typing import Any

from . import corpus
from . import ingest as ing
from .answer import answer as make_answer
from .answer import validate
from .db import Meta
from .retrieval import LexicalReranker, SearchParams, search
from .store import MemoryStore, Store
from .text import HashEmbedder

_NUM = re.compile(r"(?<![\w.#])\d[\d,]*(?:\.\d+)?%?")


def build_index(store: Store | None = None, embedder: Any = None) -> tuple[Store, Meta, Any]:
    store = store or MemoryStore()
    meta = Meta(":memory:")
    embedder = embedder or HashEmbedder()
    for d in corpus.build_kb():
        ing.ingest_markdown(store, meta, embedder, d.collection, d.markdown, d.meta)
    return store, meta, embedder


def resolve_gold(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill ``gold_citations`` by locating the chunk(s) that contain each gold fact in the gold document."""
    from .retrieval import citation_id

    out = []
    for r in rows:
        pts = store.scroll(r["collection"])
        gold: list[str] = []
        for fact in r["gold_facts"]:
            for _, pl in pts:
                if fact in pl.get("text", "") and (r["gold_doc"] is None or pl.get("doc_slug") == r["gold_doc"]):
                    cid = citation_id(pl)
                    if cid not in gold:
                        gold.append(cid)
        out.append({**r, "gold_citations": gold})
    return out


def _dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def extractive_chat(messages: list[dict[str, str]], response_format: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stand-in for the LLM: quotes the best-matching sentence of the top passage with its citation id (tests the validators)."""
    user = messages[-1]["content"]
    passages = re.findall(r'<passage id="([^"]+)">\n(.*?)\n</passage>', user, re.S)
    question = user.rsplit("Question:", 1)[-1].split("\n")[0]
    if not passages:
        return {"content": '{"answer":"","citations":[],"insufficient_evidence":true}'}
    from .text import tokenize

    qt = set(tokenize(question))
    best = None
    for cid, text in passages[:3]:
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            sent = sent.strip()
            if not sent or sent.startswith("|---") or "ignore all previous" in sent.lower():
                continue
            s = len(qt & set(tokenize(sent))) / (len(qt) or 1)
            if best is None or s > best[0]:
                best = (s, cid, sent)
    if best is None or best[0] < 0.2:
        return {"content": '{"answer":"","citations":[],"insufficient_evidence":true}'}
    import json

    return {"content": json.dumps({"answer": f"{best[2].rstrip('.')} [{best[1]}].", "citations": [best[1]], "insufficient_evidence": False}), "model": {"alias": "stub"}}


def evaluate(store: Store, embedder: Any, rows: list[dict[str, Any]], *, rerank: bool = True, chat: Any = extractive_chat, top_k: int = 5, min_score: float = 0.35,
             min_score_no_rerank: float = 0.02, sparse_only: bool = False, dense_only: bool = False) -> dict[str, Any]:
    reranker = LexicalReranker() if rerank else None
    ans_rows = [r for r in rows if r["answerable"]]
    hits5 = rr_sum = ndcg_sum = 0.0
    cite_ok = cite_total = 0
    unans_ok = unans_n = 0
    temporal_ok = temporal_n = 0
    num_ok = num_n = 0
    for r in rows:
        params = SearchParams(top_k=top_k, rerank=rerank)
        if sparse_only:
            params.dense_k = 0
        if dense_only:
            params.sparse_k = 0
        res = search(store, embedder, r["collection"], r["question"], r["filters"], params, reranker)
        ids = [x["citation_id"] for x in res.results]
        if r["answerable"]:
            gold = set(r["gold_citations"])
            rel = [1 if i in gold else 0 for i in ids[:5]]
            hits5 += 1 if any(rel) else 0
            first = next((k for k, v in enumerate(rel, start=1) if v), None)
            rr_sum += 1 / first if first else 0.0
            ideal = _dcg(sorted([1] * min(len(gold), 5), reverse=True)) or 1.0
            ndcg_sum += _dcg(rel) / ideal
            if r["type"] == "temporal_trap":
                temporal_n += 1
                temporal_ok += 1 if res.results and res.results[0]["version"] == r["expect_version"] else 0
        if chat is not None and not (sparse_only or dense_only):
            out = make_answer(r["question"], res.results, reranked=res.reranked, min_score=min_score, min_score_no_rerank=min_score_no_rerank, chat=chat)
            if r["answerable"]:
                if not out["insufficient_evidence"]:
                    cite_total += len(out["citations"])
                    cite_ok += sum(1 for c in out["citations"] if c in set(r["gold_citations"]))
                    texts = {x["citation_id"]: x["text"] for x in res.results}
                    cited = " ".join(texts.get(c, "") for c in out["citations"])
                    body = re.sub(r"\[[^\]]+\]", "", out["answer"] or "")
                    nums = [n.replace(",", "").rstrip("%") for n in _NUM.findall(body)]
                    num_n += 1
                    num_ok += 1 if all(n in cited.replace(",", "") for n in nums) else 0
            else:
                unans_n += 1
                unans_ok += 1 if out["insufficient_evidence"] else 0
    n = max(len(ans_rows), 1)
    return {
        "rows": len(rows), "answerable": len(ans_rows), "unanswerable": len(rows) - len(ans_rows),
        "recall@5": round(hits5 / n, 4), "mrr": round(rr_sum / n, 4), "ndcg@5": round(ndcg_sum / n, 4),
        "citation_precision": round(cite_ok / cite_total, 4) if cite_total else None,
        "insufficient_evidence_accuracy": round(unans_ok / unans_n, 4) if unans_n else None,
        "temporal_trap_accuracy": round(temporal_ok / temporal_n, 4) if temporal_n else None,
        "numeric_faithfulness": round(num_ok / num_n, 4) if num_n else None,
    }


__all__ = ["build_index", "evaluate", "resolve_gold", "extractive_chat", "validate"]
