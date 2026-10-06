"""JWKS cache: TTL, refetch once on unknown kid (rate limited), serve stale while Keycloak is down."""

from __future__ import annotations

import time
from typing import Any

import httpx
from jwt.algorithms import RSAAlgorithm

from app.core.errors import Unauthenticated


class JwksCache:
    def __init__(
        self,
        url: str,
        ttl: int = 600,
        stale_max: int = 3600,
        client: httpx.AsyncClient | None = None,
        min_refetch_s: float = 10.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.url, self.ttl, self.stale_max, self.min_refetch_s = url, ttl, stale_max, min_refetch_s
        self.client = client or httpx.AsyncClient(timeout=5)
        self.clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_attempt = -1e9
        self.stale = False

    def _expired(self) -> bool:
        return self._fetched_at is None or self.clock() - self._fetched_at > self.ttl

    async def _fetch(self) -> None:
        r = await self.client.get(self.url)
        r.raise_for_status()
        self._keys = {
            k["kid"]: RSAAlgorithm.from_jwk(k)
            for k in r.json()["keys"]
            if k.get("use", "sig") == "sig" and k.get("kty") == "RSA"
        }
        self._fetched_at = self.clock()
        self.stale = False

    def _can_refetch(self) -> bool:
        return self.clock() - self._last_attempt >= self.min_refetch_s

    async def get(self, kid: str) -> Any:
        if kid in self._keys and not self._expired():
            return self._keys[kid]
        if self._can_refetch():
            self._last_attempt = self.clock()
            try:
                await self._fetch()
            except (httpx.HTTPError, ValueError, KeyError):
                fresh_enough = (
                    self._fetched_at is not None
                    and self.clock() - self._fetched_at < self.stale_max
                )
                if kid in self._keys and fresh_enough:
                    self.stale = True
                    return self._keys[kid]
                raise Unauthenticated("jwks_unavailable", "identity provider unavailable") from None
        if kid in self._keys:
            return self._keys[kid]
        raise Unauthenticated("invalid_token", "unknown signing key")

    async def prefetch(self) -> None:
        try:
            await self._fetch()
        except (httpx.HTTPError, ValueError, KeyError):
            self.stale = True  # app still starts; first request retries
