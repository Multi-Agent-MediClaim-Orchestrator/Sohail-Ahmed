from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    blur_min: float = 80.0
    contrast_min: float = 0.20
    skew_max: float = 8.0
    bright_min: float = 40.0
    bright_max: float = 235.0
    det_conf_min: float = 0.5
    max_mp: int = 40
    max_pages: int = 20
    registry_url: str = ""  # hospital-api GET /v1/internal/hospitals
    registry_token_url: str = "http://localhost:8080/realms/hospital/protocol/openid-connect/token"
    registry_client_id: str = "hospital-internal"
    registry_client_secret: str = ""
    llm_base_url: str = "http://localhost:11434/v1"
    vision_model: str = "gemma4:latest"  # local only: pages are never sent to a cloud model
    esc_per_doc: int = 2
    esc_daily_cap: int = 60

    @classmethod
    def from_env(cls) -> Settings:
        g = os.environ.get
        return cls(
            registry_url=g("HOSP_API_URL", "http://localhost:8100").rstrip("/")
            + "/v1/internal/hospitals",
            registry_client_secret=g("HOSP_INTERNAL_CLIENT_SECRET", ""),
            llm_base_url=g("HOSP_LLM_BASE_URL", cls.llm_base_url),
            vision_model=g("VISION_MODEL", g("HOSP_LLM_LOCAL_MODEL", cls.vision_model)),
        )
