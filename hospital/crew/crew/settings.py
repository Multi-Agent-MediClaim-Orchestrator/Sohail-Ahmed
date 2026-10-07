"""Crew settings. The LLM stack is Ollama only (decision 2026-10): a general model for masked text, a local
model for anything that might carry identity. No provider keys."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = "http://localhost:11434/v1"
    model_general: str = "gemma4:31b-cloud"
    model_local: str = "gemma4:latest"
    api_url: str = "http://localhost:8100"
    token_url: str = "http://localhost:8080/realms/hospital/protocol/openid-connect/token"  # noqa: S105
    client_id: str = "hospital-crew"
    client_secret: str = ""
    concurrency: int = 3
    llm_mode: str = "ollama"  # ollama | rules (deterministic, no model)
    max_tokens_job: int = 30000
    job_ttl_s: int = 86400
    llm_timeout_s: float = 120.0
    prompt_dir: Path = field(default_factory=lambda: Path(__file__).parent / "prompts")
    prompt_pins: dict[str, str] = field(default_factory=dict)
    rag_url: str = ""  # rag-service for the admissible-amount estimate (hosp_insurer_rules); empty = estimate unavailable
    rag_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        g = os.environ.get
        pins = {
            k[len("CREW_PROMPT_PIN_") :].lower(): v
            for k, v in os.environ.items()
            if k.startswith("CREW_PROMPT_PIN_")
        }
        return cls(
            llm_base_url=g("HOSP_LLM_BASE_URL", cls.llm_base_url),
            model_general=g("HOSP_LLM_MODEL", cls.model_general),
            model_local=g("HOSP_LLM_LOCAL_MODEL", cls.model_local),
            api_url=g("HOSP_API_URL", cls.api_url),
            token_url=g("KEYCLOAK_TOKEN_URL", cls.token_url),
            client_secret=g("HOSP_CREW_CLIENT_SECRET", ""),
            concurrency=int(g("CREW_CONCURRENCY", "3")),
            llm_mode=g("CREW_LLM", "ollama"),
            prompt_pins=pins,
            rag_url=g("HOSP_RAG_URL", ""),
            rag_token=g("HOSP_RAG_TOKEN", "") or rag_token_from_secret(g("RAG_JWT_SECRET", "")),
        )


def rag_token_from_secret(secret: str, ttl: int = 86400) -> str:
    """A rag-service service token for `hospital-crew` (same claims as rag_service.security.issue_token), so a local run
    needs only RAG_JWT_SECRET from .env. The token can read hosp_* collections only."""
    if not secret:
        return ""
    import time

    import jwt

    t = int(time.time())
    claims = {
        "svc": "hospital-crew",
        "system": "hospital",
        "role": "service",
        "iat": t,
        "exp": t + ttl,
        "case_scope": True,
    }
    return jwt.encode(claims, secret, algorithm="HS256")
