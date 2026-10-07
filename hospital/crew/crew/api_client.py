"""The crew's only channel to the hospital API: a service token (client credentials) and four calls.
Crews never touch the database."""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx


class ApiCallFailed(Exception):
    """The hospital API refused a call; keep its answer so the job's error says why (a bare 422 hid a real cause)."""


def _check(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise ApiCallFailed(
            f"{r.request.method} {r.request.url.path} -> {r.status_code} {r.text[:300]}"
        )


class ApiClient(Protocol):
    async def get(self, path: str) -> dict[str, Any]: ...
    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class HttpApi:
    def __init__(
        self,
        base: str,
        token_url: str,
        client_id: str,
        secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base, self.token_url, self.cid, self.secret = (
            base.rstrip("/"),
            token_url,
            client_id,
            secret,
        )
        self.client = client or httpx.AsyncClient(timeout=15)
        self._tok: tuple[str, float] | None = None

    async def _token(self) -> str:
        if self._tok and self._tok[1] > time.time() + 30:
            return self._tok[0]
        r = await self.client.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cid,
                "client_secret": self.secret,
            },
        )
        r.raise_for_status()
        j = r.json()
        self._tok = (j["access_token"], time.time() + j.get("expires_in", 60))
        return self._tok[0]

    async def _h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}"}

    async def get(self, path: str) -> dict[str, Any]:
        r = await self.client.get(self.base + path, headers=await self._h())
        _check(r)
        return r.json()  # type: ignore[no-any-return]

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self.client.post(self.base + path, json=body, headers=await self._h())
        _check(r)
        return r.json() if r.content else {}  # type: ignore[no-any-return]
