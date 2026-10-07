"""Typed settings (env prefix ``INS_``). Secrets come only from env — never from code or the database."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INS_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://ins_app:ins_app_dev@localhost:5433/insurer"
    owner_database_url: str = "postgresql://postgres:postgres@localhost:5433/insurer"
    db_pool_size: int = 10
    db_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # --- contract / hmac ---
    hmac_secrets: dict[str, list[str]] = Field(default_factory=lambda: {"hosp-001": ["dev-hosp-to-ins-secret-0000000000000000"], "bank-sim": ["dev-bank-sim-callback-secret-00000000000"]})
    ins_to_hosp_hmac_secret: str = "dev-ins-to-hosp-secret-0000000000000000"
    ins_key_id: str = "ins-001"
    clock_skew_seconds: int = 300
    rate_limit_per_min: int = 100
    hospital_callback_base: str = "http://localhost:8000"  # fallback when network_hospital.callback_base_url is NULL

    # --- documents ---
    allowed_doc_hosts: str = "minio:9000,hospital-minio:9000,localhost:9000"
    max_doc_bytes: int = 52_428_800
    url_enc_key: str = ""  # Fernet key; generated per-process in dev when empty
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "insurer-docs"
    clamav_host: str = ""
    clamav_port: int = 3310
    presign_ttl_seconds: int = 900

    # --- orchestration ---
    n8n_url: str = "http://localhost:5679"
    n8n_webhook_secret: str = "dev-n8n-webhook-secret"
    n8n_service_token: str = ""
    crew_url: str = ""  # empty = no crew: steps run rules-only (no degraded flag)
    orchestrator: str = "inline"  # inline | n8n
    jobs_mode: str = "inline"  # inline (asyncio tasks in this process) | arq (needs the worker and arq:* keys) | manual (tests)
    run_dispatcher: bool = True  # outbox -> hospital callbacks every second
    run_cron: bool = True  # SLA tick / settlement auto-close
    crew_timeout_seconds: int = 90
    calc_engine_url: str = "inprocess"  # or http://calc-engine:8120
    calc_remote: bool = False
    calc_engine_jwt_secret: str = "dev-calc-engine-jwt-secret-000000000000"
    rag_url: str = "http://localhost:8400"

    # --- auth (Keycloak / dev tokens) ---
    keycloak_issuer: str = ""
    keycloak_audience: str = "insurer-api"
    keycloak_jwks_url: str = ""
    dev_jwt_secret: str = "dev-insurer-api-jwt-secret-0000000000000000"  # HS256 tokens when no JWKS configured (dev/test only)
    service_token_audience: str = "insurer-api"
    allow_dev_tokens: bool = False  # accept HS256 dev tokens even when Keycloak is configured (local demos and the e2e script only)

    # --- decision gate ---
    segregation_of_duties: bool = True
    allow_auto_approval: bool = True  # user decision: auto-approve clean claims <= T_auto
    allow_reviewer_tier_final: bool = True
    decision_sla_hours: int = 24
    sse_max_connections_per_user: int = 5
    sse_replay_events: int = 1000
    sse_heartbeat_seconds: int = 15
    events_stream: str = "ins:events"

    # --- queries ---
    query_text_max: int = 1200
    query_regen_max: int = 3

    # --- settlement (simulation only) ---
    settlement_mode: str = "sim"
    sim_profile: str | None = None  # default tpa-sim bank profile (always_pay, fail_once_then_pay, ...)
    tpa_sim_url: str = "http://localhost:8500"
    bank_sim_hmac_secret: str = "dev-bank-sim-secret-000000000000000000"
    bank_key_id: str = "ins-bank-001"
    settlement_max_attempts: int = 3
    settlement_retry_base_minutes: int = 5
    autoclose_days: int = 7
    allow_simulated_reversal: bool = True
    report_tz: str = "Asia/Kolkata"

    # --- assignment / sla ---
    assignment_strategy: str = "least_loaded"
    sla_tick_seconds: int = 300

    @field_validator("hmac_secrets", mode="before")
    @classmethod
    def _parse_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("settlement_mode")
    @classmethod
    def _sim_only(cls, v: str) -> str:
        if v != "sim":
            raise ValueError("INS_SETTLEMENT_MODE must be 'sim': this project never talks to a real bank")
        return v

    def secrets_for(self, key_id: str) -> list[bytes] | None:
        s = self.hmac_secrets.get(key_id)
        return [x.encode() for x in s] if s else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
