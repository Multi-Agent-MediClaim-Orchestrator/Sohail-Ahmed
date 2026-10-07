"""insurer-crew HTTP client. Canonical paths live here and nowhere else (03-05 §4.4 reconciliation note)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from ..settings import Settings, get_settings

PATH_IDENTITY = "/v1/identity/analyze"
PATH_AUTHENTICITY = "/v1/authenticity/analyze"
PATH_COVERAGE = "/v1/coverage/analyze"
PATH_CALC_MAP = "/v1/calc/map-lines"
PATH_QUERY_DRAFT = "/v1/query/draft"
PATH_QUERY_TRIAGE = "/v1/query/triage"
PATH_SUPERVISOR = "/v1/supervisor/summarize"


class CrewUnavailable(Exception):
    """Crew down, timed out, busy or the LLM is unavailable -> degrade (templates / manual verification)."""


class CrewInvalidOutput(Exception):
    pass


_transport: httpx.AsyncBaseTransport | None = None


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).crew_url) or _transport is not None


async def call(path: str, case_id: Any, context: dict[str, Any], *, request_id: uuid.UUID | None = None, settings: Settings | None = None,
               options: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    body = {"request_id": str(request_id or uuid.uuid4()), "case_id": str(case_id), "context": context, "options": options or {}}
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(transport=_transport, base_url=s.crew_url or "http://crew", timeout=s.crew_timeout_seconds) as c:
                r = await c.post(path, json=body, headers={"X-Service-Token": s.n8n_service_token or "dev"})
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
            await asyncio.sleep(0.05 * (2**attempt))
            continue
        if r.status_code == 200:
            return r.json()  # type: ignore[no-any-return]
        if r.status_code == 422:
            raise CrewInvalidOutput(r.text[:300])
        last = CrewUnavailable(f"{r.status_code}")
        if r.status_code in (429, 503, 504, 500):
            await asyncio.sleep(0.05 * (2**attempt))
            continue
        break
    raise CrewUnavailable(str(last))
