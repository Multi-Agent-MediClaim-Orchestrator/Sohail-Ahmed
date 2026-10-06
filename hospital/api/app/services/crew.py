"""Claim-builder crew client. The crew (doc 09) never writes the database: it receives a job, and posts its
draft back to the API's internal endpoint."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

log = logging.getLogger("app.crew")


class CrewClient(Protocol):
    async def start_claim_build(self, job: dict[str, Any]) -> bool: ...


class HttpCrew:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.base, self.client = base_url.rstrip("/"), client or httpx.AsyncClient(timeout=10)

    async def start_claim_build(self, job: dict[str, Any]) -> bool:
        try:
            r = await self.client.post(f"{self.base}/v1/jobs/claim-build", json=job)
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("crew unreachable: %s", type(e).__name__)
            return False


class RecordingCrew:
    """Test double. `fail=True` simulates the crew being down; `on_job` may react to a job (e.g. post a draft)."""

    def __init__(self, fail: bool = False) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.fail = fail

    async def start_claim_build(self, job: dict[str, Any]) -> bool:
        if self.fail:
            return False
        self.jobs.append(job)
        return True
