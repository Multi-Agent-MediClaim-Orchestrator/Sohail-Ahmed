"""``uvicorn rag_service.main:app --port 8400`` - wires Qdrant/gateway from the environment.

``STORE=memory`` + ``EMBEDDER=hash`` gives a no-dependency demo; ``STORE=qdrant`` + ``EMBEDDER=gateway`` is the real stack."""

from __future__ import annotations

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
    chat = GatewayChat(cfg.llm_gateway_url, cfg.llm_gateway_key) if live else None
    return create_app(cfg, store=store, meta=Meta(cfg.db_url), embedder=embedder, chat=chat, loader=_minio_loader(cfg), docpipe=_docpipe(cfg))


app = build()
