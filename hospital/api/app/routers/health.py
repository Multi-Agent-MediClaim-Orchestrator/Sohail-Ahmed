from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/v1/health", operation_id="health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/v1/ready", operation_id="ready")
async def ready(request: Request) -> Any:
    checks: dict[str, str] = {}
    extra: dict[str, Any] = {}
    app = request.app
    try:
        async with app.state.sessionmaker() as s:
            await asyncio.wait_for(s.execute(text("SELECT 1")), 0.5)
            head = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        checks["db"] = "ok"
        checks["schema"] = "ok" if head == app.state.code_head else "schema_out_of_date"
    except Exception:  # noqa: BLE001
        checks["db"] = "down"
    try:
        await app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "down"
    jw = app.state.jwks
    if jw._keys:  # noqa: SLF001
        checks["jwks"] = "ok"
        if jw.stale:
            extra["auth_jwks_stale"] = True
    else:
        checks["jwks"] = "down"
    bad = [k for k, v in checks.items() if v != "ok"]
    body = {"status": "ok" if not bad else "unavailable", "checks": checks, **extra}
    return JSONResponse(body, status_code=200 if not bad else 503)
