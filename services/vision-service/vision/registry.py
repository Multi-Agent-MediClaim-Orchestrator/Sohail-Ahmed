from __future__ import annotations

import time
from typing import Any

import httpx

from vision.settings import Settings


class Registry:
    """Hospital registry from hospital-api, cached 10 minutes. Failure means 'no registry', never an exception."""

    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s, self.c = s, client or httpx.AsyncClient(timeout=10)
        self._cache: tuple[float, list[dict[str, Any]]] = (0.0, [])

    async def get(self) -> list[dict[str, Any]]:
        if time.time() - self._cache[0] < 600 and self._cache[1]:
            return self._cache[1]
        try:
            t = await self.c.post(
                self.s.registry_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.s.registry_client_id,
                    "client_secret": self.s.registry_client_secret,
                },
            )
            t.raise_for_status()
            r = await self.c.get(
                self.s.registry_url, headers={"Authorization": f"Bearer {t.json()['access_token']}"}
            )
            r.raise_for_status()
            items = r.json()["items"]
        except (httpx.HTTPError, KeyError, ValueError):
            return self._cache[1]
        self._cache = (time.time(), items)
        return items  # type: ignore[no-any-return]
