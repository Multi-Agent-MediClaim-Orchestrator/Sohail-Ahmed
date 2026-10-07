"""Run the retrieval/answer evaluation (04-05 task 19) and write JSON + markdown reports.

    python scripts/eval_retrieval.py [--out data/eval/reports] [--build-set data/eval/rag_qa.jsonl]

Offline mode (hash embedder, lexical reranker, extractive answer stub) unless pointed at a live stack; the report records which.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from rag_service import corpus
from rag_service import evaluation as ev

TARGETS = {"recall@5": 0.85, "mrr": 0.6, "ndcg@5": 0.7, "citation_precision": 0.9, "insufficient_evidence_accuracy": 0.9, "temporal_trap_accuracy": 0.95, "numeric_faithfulness": 1.0}
VARIANTS = [("hybrid", False, {}), ("hybrid+rerank", True, {}), ("dense_only", False, {"dense_only": True}), ("sparse_only", False, {"sparse_only": True})]
MODE = "offline: hash embedder + lexical reranker + extractive answer stub"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/eval/reports")
    ap.add_argument("--build-set", default="data/eval/rag_qa.jsonl")
    a = ap.parse_args()
    store, _, emb = ev.build_index()
    rows = ev.resolve_gold(store, corpus.build_qa())
    p = Path(a.build_set)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    full = ev.evaluate(store, emb, rows)
    abl = {name: ev.evaluate(store, emb, rows, rerank=rr, chat=None, **kw) for name, rr, kw in VARIANTS}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"rag_eval_{stamp}.json").write_text(json.dumps({"metrics": full, "ablation": abl, "targets": TARGETS, "mode": MODE}, indent=1), encoding="utf-8")
    lines = [f"# RAG evaluation {stamp}", "", f"Mode: {MODE}. Re-run against the live gateway for real numbers.", "", "| metric | value | target | pass |", "|---|---|---|---|"]
    for k, t in TARGETS.items():
        v = full.get(k)
        ok = "yes" if v is not None and v >= t else "NO"
        lines.append(f"| {k} | {v} | >= {t} | {ok} |")
    lines += ["", "## Ablation (retrieval only)", "", "| variant | recall@5 | mrr | ndcg@5 |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['recall@5']} | {v['mrr']} | {v['ndcg@5']} |" for k, v in abl.items()]
    (out / f"rag_eval_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
