"""``uvicorn rag_service.main:app --port 8400`` - wires Qdrant/gateway from the environment.

``STORE=memory`` + ``EMBEDDER=hash`` gives a no-dependency demo; ``STORE=qdrant`` + ``EMBEDDER=gateway`` is the real stack."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from .app import create_app
from .config import Settings, get_settings
from .db import Meta
from .gateway import GatewayChat, GatewayEmbedder
from .qdrant_store import QdrantStore
from .store import MemoryStore
from .text import HashEmbedder


def _minio_loader(cfg: Settings) -> Callable[[str, str], bytes] | None:
    if not cfg.minio_access_key:
        return None
    from minio import Minio

    mc = Minio(cfg.minio_endpoint, cfg.minio_access_key, cfg.minio_secret_key, secure=False)

    def load(bucket: str, key: str) -> bytes:
        r = mc.get_object(bucket, key)
        try:
            return r.read()
        finally:
            r.close()

    return load


def _docpipe(cfg: Settings) -> Callable[[str, bytes], str] | None:
    if not cfg.docpipe_url:
        return None
    import httpx

    def parse(filename: str, data: bytes) -> str:
        r = httpx.post(f"{cfg.docpipe_url}/v1/parse", files={"file": (filename, data)}, timeout=600)
        r.raise_for_status()
        return r.json()["markdown"]

    return parse


def build():
    cfg = get_settings()
    store = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key) if cfg.store == "qdrant" else MemoryStore()
    live = cfg.embedder == "gateway"
    embedder = GatewayEmbedder(cfg.llm_gateway_url, cfg.llm_gateway_key, cfg.embed_alias, cfg.embed_batch) if live else HashEmbedder(cfg.embed_dim)
    if live:  # learn the embedding model id now (as seed_kb does), or the first search is refused as a model mismatch
        try:
            embedder.embed(["probe"], kind="query")
        except Exception as exc:  # noqa: BLE001  (model server not up yet: the id is learned on the first real call)
            logging.getLogger("rag").warning("embedding probe failed at start-up: %s", exc)
    chat = GatewayChat(cfg.llm_gateway_url, cfg.llm_gateway_key, cfg.chat_alias, reasoning_effort=cfg.chat_reasoning_effort) if live else None
    meta = Meta(cfg.db_url)
    if cfg.store == "memory" and os.environ.get("RAG_SEED_ON_START") == "1":  # offline demo: no Qdrant, no embedding model
        from . import corpus
        from . import ingest as ing

        for d in corpus.build_kb():
            ing.ingest_markdown(store, meta, embedder, d.collection, d.markdown, d.meta, supersede=True)
        logging.getLogger("rag").warning("seeded the synthetic knowledge base in memory (%s documents)", len(corpus.build_kb()))
    return create_app(cfg, store=store, meta=meta, embedder=embedder, chat=chat, loader=_minio_loader(cfg), docpipe=_docpipe(cfg))


app = build()
