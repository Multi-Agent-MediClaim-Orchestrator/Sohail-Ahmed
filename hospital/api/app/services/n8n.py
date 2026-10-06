"""n8n webhook client. Failures never fail the request: the sweeper re-triggers pending documents."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

log = logging.getLogger("app.n8n")


class N8nClient(Protocol):
    async def trigger(self, path: str, payload: dict[str, Any], idem: str) -> bool: ...


class HttpN8n:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=5)

    async def trigger(self, path: str, payload: dict[str, Any], idem: str) -> bool:
        try:
            r = await self.client.post(
                f"{self.base}/{path}", json=payload, headers={"X-Idempotency-Key": idem}
            )
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("n8n trigger failed: %s", type(e).__name__)
            return False


class RecordingN8n:
    """Test double: records calls; `fail=True` simulates n8n being down."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.fail = fail

    async def trigger(self, path: str, payload: dict[str, Any], idem: str) -> bool:
        if self.fail:
            return False
        self.calls.append((path, payload, idem))
        return True
