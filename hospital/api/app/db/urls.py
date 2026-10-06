"""Database URLs. Reads env vars, falling back to the repo-root .env for local development."""

import os
import pathlib
from urllib.parse import quote

_ROOT_ENV = pathlib.Path(__file__).resolve().parents[4] / ".env"


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    if _ROOT_ENV.exists():
        for line in _ROOT_ENV.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values[k] = v
    values.update({k: v for k, v in os.environ.items() if k.startswith(("HOSP_", "N8N_"))})
    return values


def _url(driver: str, user: str, pw_key: str, db: str | None = None) -> str:
    e = _env()
    host = e.get("HOSP_DB_HOST", "localhost")
    port = e.get("HOSP_DB_PORT", "5432")
    name = db or e.get("HOSP_DB_NAME", "hospital")
    return f"postgresql+{driver}://{user}:{quote(e[pw_key])}@{host}:{port}/{name}"


def owner_url(db: str | None = None) -> str:
    """Alembic (sync psycopg) as hosp_owner. HOSP_DB_OWNER_URL overrides."""
    return os.environ.get("HOSP_DB_OWNER_URL") or _url("psycopg", "hosp_owner", "HOSP_OWNER_PW", db)


def app_url(db: str | None = None) -> str:
    return os.environ.get("HOSP_DB_URL") or _url("asyncpg", "hosp_app", "HOSP_APP_PW", db)


def readonly_url(db: str | None = None) -> str:
    return _url("psycopg", "hosp_readonly", "HOSP_READONLY_PW", db)


def superuser_url(db: str = "postgres") -> str:
    e = _env()
    return (
        f"postgresql+psycopg://{e.get('HOSP_DB_USER', 'hospital')}:{quote(e['HOSP_DB_PASSWORD'])}"
        f"@{e.get('HOSP_DB_HOST', 'localhost')}:{e.get('HOSP_DB_PORT', '5432')}/{db}"
    )


def app_sync_url(db: str | None = None) -> str:
    """hosp_app over sync psycopg (tests, scripts)."""
    return _url("psycopg", "hosp_app", "HOSP_APP_PW", db)


def n8n_url() -> str:
    return _url("psycopg", "n8n_app", "N8N_APP_PW", "hospital")
