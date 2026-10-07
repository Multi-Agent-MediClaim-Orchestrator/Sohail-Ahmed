from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    rag_port: int = 8400
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    llm_gateway_url: str = "http://llm-gateway:4000"
    llm_gateway_key: str = ""
    docpipe_url: str = "http://doc-pipeline:8200"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    kb_bucket: str = "kb-sources"
    embed_alias: str = "embed"
    embed_batch: int = 16
    embed_dim: int = 768
    chunk_tokens: int = 600
    chunk_overlap: int = 80
    chunk_min_tokens: int = 120
    chunk_max_tokens: int = 750
    dense_k: int = 30
    sparse_k: int = 30
    fuse_k: int = 60
    rerank_input: int = 20
    rerank: str = "on"
    rerank_cpu_ceiling: int = 85
    min_score: float = 0.35
    min_score_no_rerank: float = 0.02
    max_top_k: int = 20
    max_context_tokens: int = 6000
    case_context_ttl_days: int = 7
    jwt_secret: str = "dev-rag-jwt-secret-000000000000000000000000"
    db_url: str = "data/rag.db"  # sqlite path (own volume, independent of insurer-db)
    store: str = "memory"  # memory | qdrant
    embedder: str = "hash"  # hash | gateway
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()
