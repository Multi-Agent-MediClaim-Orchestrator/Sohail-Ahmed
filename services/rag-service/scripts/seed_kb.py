"""Build the synthetic knowledge base (make seed-kb). Uses the configured store/embedder; STORE=memory only validates the corpus.

    python scripts/seed_kb.py
"""

from __future__ import annotations

from rag_service import corpus
from rag_service import ingest as ing
from rag_service.config import get_settings
from rag_service.db import Meta
from rag_service.gateway import GatewayEmbedder
from rag_service.qdrant_store import QdrantStore
from rag_service.store import MemoryStore
from rag_service.text import HashEmbedder


def main() -> int:
    cfg = get_settings()
    store = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key) if cfg.store == "qdrant" else MemoryStore()
    live = cfg.embedder == "gateway"
    emb = GatewayEmbedder(cfg.llm_gateway_url, cfg.llm_gateway_key, cfg.embed_alias, cfg.embed_batch) if live else HashEmbedder(cfg.embed_dim)
    if live:
        emb.embed(["probe"], kind="query")
    meta = Meta(cfg.db_url)
    for d in corpus.build_kb():
        rep = ing.ingest_markdown(store, meta, emb, d.collection, d.markdown, d.meta, supersede=True)
        print(f"{d.collection:24s} {d.slug:18s} added={rep.chunks_added} skipped={rep.chunks_skipped} tables={rep.tables_found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
