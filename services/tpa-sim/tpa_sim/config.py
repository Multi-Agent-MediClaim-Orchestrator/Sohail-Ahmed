from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class SimSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TPA_SIM_", env_file=".env", extra="ignore")

    port: int = 8500
    db_url: str = "sqlite+aiosqlite:///data/tpa_sim.db"
    hosp_base_url: str = "http://localhost:8000"
    ins_base_url: str = "http://localhost:8100"  # insurer-api (bank callbacks)
    hosp_keys: dict[str, str] = {"hosp-001": "dev-hosp-to-ins-secret-0000000000000000"}  # key id -> secret (HOSP_TO_INS)
    ins_to_hosp_secret: str = "dev-ins-to-hosp-secret-0000000000000000"
    ins_key_id: str = "ins-001"
    default_scenario: str = "happy_path"
    default_profile: str = "default"
    time_scale: float = 1.0
    retry_scale: float = 1.0
    expose_scenario: bool = True
    verify_docs: bool = False
    llm: bool = False
    admin_token: str = ""
    chaos_seed: int = 42
    rate_limit: int = 100
    scenarios_dir: str = str(ROOT / "scenarios")
    bank_secret: str = "dev-bank-sim-secret-000000000000000000"
    bank_callback_secret: str = "dev-bank-sim-callback-secret-00000000000"
    log_cap: int = 20_000

    @field_validator("hosp_keys", mode="before")
    @classmethod
    def _keys(cls, v: object) -> object:
        if isinstance(v, str):
            import json

            try:
                return json.loads(v)
            except ValueError:
                return dict(p.split(":", 1) for p in v.split(",") if ":" in p)
        return v


@lru_cache
def get_settings() -> SimSettings:
    return SimSettings()
