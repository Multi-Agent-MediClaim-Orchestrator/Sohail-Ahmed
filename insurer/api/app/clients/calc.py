"""calc-engine client: in-process by default (pure library), HTTP when ``INS_CALC_ENGINE_URL`` is set to a service URL."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from calc_engine.engine import run as run_engine
from calc_engine.models import CalcInput, CalcResult
from claim_contract.errors import ProblemError

from ..settings import Settings, get_settings

_transport: httpx.AsyncBaseTransport | None = None  # tests may inject an ASGI transport


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def _token(s: Settings) -> str:
    now = int(time.time())
    return jwt.encode({"sub": "svc-insurer-api", "aud": "calc-engine", "iat": now, "exp": now + 60}, s.calc_engine_jwt_secret, algorithm="HS256")


async def calculate(inp: CalcInput, settings: Settings | None = None) -> CalcResult:
    s = settings or get_settings()
    if not s.calc_engine_url.startswith("http"):
        return run_engine(inp)
    async with httpx.AsyncClient(transport=_transport, base_url=s.calc_engine_url, timeout=30) as c:
        r = await c.post("/v1/calculate", content=inp.model_dump_json(), headers={"Authorization": f"Bearer {_token(s)}", "Content-Type": "application/json"})
    if r.status_code != 200:
        raise ProblemError("service_unavailable", f"calc-engine returned {r.status_code}", retryable=r.status_code >= 500)
    return CalcResult.model_validate(r.json())


def input_dump(inp: CalcInput) -> dict[str, Any]:
    import json

    return json.loads(inp.model_dump_json())
