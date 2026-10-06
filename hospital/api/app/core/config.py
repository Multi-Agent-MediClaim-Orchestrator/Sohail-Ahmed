"""Settings: HOSP_* environment variables, falling back to the repo-root .env for local dev."""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote

from pydantic import BaseModel, Field

from app.db.urls import _env, app_url


class Settings(BaseModel):
    # database / redis
    db_url: str
    redis_url: str
    # auth
    oidc_issuer: str = "http://localhost:8080/realms/hospital"
    oidc_jwks_url: str = "http://localhost:8080/realms/hospital/protocol/openid-connect/certs"
    oidc_audience: str = "hospital-api"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    jwt_leeway_s: int = 30
    jwks_ttl_s: int = 600
    jwks_stale_max_s: int = 3600
    deny_audit_window_s: int = 60
    user_active_cache_s: int = 30
    # storage / scanning
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "hosp_svc"
    minio_secret_key: str = ""
    minio_secure: bool = False
    minio_bucket: str = "hospital-docs"
    clamav_host: str = "localhost"
    clamav_port: int = 3310
    max_upload_mb: int = 25
    max_files_per_case: int = 60
    max_pdf_pages: int = 200
    presign_ttl_s: int = 300
    upload_rate_per_min: int = 30
    name_match_min: int = 90
    # integrations
    n8n_webhook_base: str = "http://localhost:5678/webhook"
    insurer_base_url: str = "http://localhost:8100"
    hospital_key_id: str = "hosp-001"
    hospital_to_insurer_secret: str = ""
    insurer_to_hospital_secret: str = ""
    insurer_key_id: str = "ins-001"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "gemma4:31b-cloud"
    llm_local_model: str = "gemma4:latest"
    field_key_b64: str = ""
    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        e = _env()
        g = lambda k, d="": os.environ.get(k, e.get(k, d))  # noqa: E731
        redis_port = g("SHARED_REDIS_PORT", "6379")
        redis_url = g("HOSP_REDIS_URL") or (
            f"redis://hosp_app:{quote(g('HOSP_REDIS_PW'))}@localhost:{redis_port}/3"
        )
        data: dict[str, object] = {
            "db_url": app_url(),
            "redis_url": redis_url,
            "minio_endpoint": f"localhost:{g('SHARED_MINIO_PORT', '9000')}",
            "minio_secret_key": g("HOSP_MINIO_SECRET"),
            "hospital_to_insurer_secret": g("HOSP_TO_INS_HMAC_SECRET"),
            "insurer_to_hospital_secret": g("INS_TO_HOSP_HMAC_SECRET"),
            "hospital_key_id": g("HOSP_KEY_ID", "hosp-001"),
            "llm_base_url": g("HOSP_LLM_BASE_URL", "http://localhost:11434/v1"),
            "llm_model": g("HOSP_LLM_MODEL", "gemma4:31b-cloud"),
            "llm_local_model": g("HOSP_LLM_LOCAL_MODEL", "gemma4:latest"),
            "field_key_b64": g("HOSP_FIELD_KEY"),
        }
        for k in (
            "oidc_issuer",
            "oidc_jwks_url",
            "oidc_audience",
            "n8n_webhook_base",
            "insurer_base_url",
            "log_level",
        ):
            if (v := os.environ.get("HOSP_" + k.upper())) is not None:
                data[k] = v
        data.update(overrides)
        return cls(**data)  # type: ignore[arg-type]


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
