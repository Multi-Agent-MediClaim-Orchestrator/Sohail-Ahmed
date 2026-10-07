"""Create all collections + payload indexes in Qdrant and record the embed model (04-05 task 3). Idempotent.

    QDRANT_URL=http://localhost:6333 QDRANT_API_KEY=... LLM_GATEWAY_KEY=... python scripts/bootstrap_collections.py
"""

from __future__ import annotations

from rag_service.app import COLLECTIONS
from rag_service.config import get_settings
from rag_service.db import Meta
from rag_service.gateway import GatewayEmbedder
from rag_service.qdrant_store import QdrantStore


def main() -> int:
    cfg = get_settings()
    q = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key)
    if not q.wait_ready(60):
        raise SystemExit("qdrant not ready")
    emb = GatewayEmbedder(cfg.llm_gateway_url, cfg.llm_gateway_key, cfg.embed_alias, cfg.embed_batch)
    emb.embed(["probe"], kind="query")  # fills emb.model_id from the x-embed-model header
    meta = Meta(cfg.db_url)
    for c in COLLECTIONS:
        phys = q.ensure_collection(c, emb.dim)
        if meta.get_collection(c) is None:
            meta.set_collection(c, emb.model_id, emb.dim, cfg.chunk_tokens, cfg.chunk_overlap)
        print(f"ok {c} -> {phys} ({emb.model_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
