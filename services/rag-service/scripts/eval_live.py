"""Live retrieval/answer evaluation: Qdrant + real embeddings + a real answering model, all local (Ollama).

    uv run python services/rag-service/scripts/eval_live.py [--out data/eval/reports] [--chat-model gemma4:latest] [--limit N]

Same questions, metrics and targets as eval_retrieval.py (which is offline: hash embedder, lexical reranker, extractive stub),
but the index is built in Qdrant from nomic-embed-text vectors and answers come from the chat model. The report records the mode,
so the two sets of numbers cannot be confused. Needs Qdrant (`docker compose ... --profile rag up -d qdrant`) and Ollama.
Environment: QDRANT_URL (default http://localhost:6333), QDRANT_API_KEY, OLLAMA_URL (default http://localhost:11434)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from eval_retrieval import (  # noqa: E402  (same targets and ablation variants as the offline run)
    TARGETS,
    VARIANTS,
)
from rag_service import corpus  # noqa: E402
from rag_service import evaluation as ev  # noqa: E402
from rag_service import ingest as ing  # noqa: E402
from rag_service.db import Meta  # noqa: E402
from rag_service.gateway import GatewayChat, GatewayEmbedder  # noqa: E402
from rag_service.qdrant_store import QdrantStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/eval/reports")
    ap.add_argument("--chat-model", default="gemma4:latest")
    ap.add_argument("--embed-model", default="nomic-embed-text:latest")
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N questions (smoke test)")
    ap.add_argument("--reuse-index", action="store_true", help="do not re-ingest the corpus")
    a = ap.parse_args()
    qdrant = os.environ.get("QDRANT_URL", "http://localhost:6333")
    ollama = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    store = QdrantStore(qdrant, os.environ.get("QDRANT_API_KEY", ""))
    emb = GatewayEmbedder(ollama, "ollama", a.embed_model, batch=16)  # ingest and retrieval add the nomic task prefixes themselves
    emb.model_id = a.embed_model
    meta = Meta(":memory:")
    t0 = time.time()
    if not a.reuse_index:
        for d in corpus.build_kb():
            rep = ing.ingest_markdown(store, meta, emb, d.collection, d.markdown, d.meta, supersede=True)
            print(f"  ingest {d.collection:24s} {d.slug:18s} added={rep.chunks_added} skipped={rep.chunks_skipped}", flush=True)
    print(f"index ready in {time.time() - t0:.0f}s", flush=True)
    rows = ev.resolve_gold(store, corpus.build_qa())
    if a.limit:
        rows = rows[: a.limit]
    chat = GatewayChat(ollama, "ollama", a.chat_model, reasoning_effort="none")
    t1 = time.time()
    full = ev.evaluate(store, emb, rows, chat=chat)
    print(f"answers evaluated in {time.time() - t1:.0f}s", flush=True)
    abl = {name: ev.evaluate(store, emb, rows, rerank=rr, chat=None, **kw) for name, rr, kw in VARIANTS}
    mode = f"live: Qdrant + {a.embed_model} + lexical reranker + {a.chat_model} answers ({len(rows)} questions)"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"rag_eval_live_{stamp}.json").write_text(json.dumps({"metrics": full, "ablation": abl, "targets": TARGETS, "mode": mode}, indent=1), encoding="utf-8")
    lines = [f"# RAG evaluation (live) {stamp}", "", f"Mode: {mode}.", "", "| metric | value | target | pass |", "|---|---|---|---|"]
    for k, t in TARGETS.items():
        v = full.get(k)
        lines.append(f"| {k} | {v} | >= {t} | {'yes' if v is not None and v >= t else 'NO'} |")
    lines += ["", "## Ablation (retrieval only)", "", "| variant | recall@5 | mrr | ndcg@5 |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['recall@5']} | {v['mrr']} | {v['ndcg@5']} |" for k, v in abl.items()]
    (out / f"rag_eval_live_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
