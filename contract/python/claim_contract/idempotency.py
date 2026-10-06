"""Idempotency (01-01 section 5). Pluggable store: in-memory for tests, Redis+DB in services."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from claim_contract.errors import ContractError


def request_hash(method: str, target: str, body: bytes) -> str:
    return hashlib.sha256(method.upper().encode() + target.encode() + body).hexdigest()


@dataclass
class StoredResponse:
    request_hash: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> StoredResponse | None: ...
    async def put(self, key: str, rec: StoredResponse) -> None: ...
    async def acquire_lock(self, key: str) -> bool: ...
    async def release_lock(self, key: str) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, StoredResponse] = {}
        self.locks: set[str] = set()

    async def get(self, key: str) -> StoredResponse | None:
        return self.records.get(key)

    async def put(self, key: str, rec: StoredResponse) -> None:
        self.records[key] = rec

    async def acquire_lock(self, key: str) -> bool:
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    async def release_lock(self, key: str) -> None:
        self.locks.discard(key)


async def run_idempotent(
    store: IdempotencyStore,
    key_id: str,
    idem_key: str,
    req_hash: str,
    execute: Callable[[], Awaitable[StoredResponse]],
) -> tuple[StoredResponse, bool]:
    """Return (response, replayed). Stores 2xx and 4xx results, never 5xx."""
    k = f"{key_id}:{idem_key}"
    rec = await store.get(k)
    if rec is not None:
        if rec.request_hash != req_hash:
            raise ContractError("idempotency_conflict", "same key used with a different request")
        return rec, True
    if not await store.acquire_lock(k):
        raise ContractError("idempotency_in_progress", "first request still running")
    try:
        resp = await execute()
        resp.request_hash = req_hash
        if resp.status < 500:
            await store.put(k, resp)
        return resp, False
    finally:
        await store.release_lock(k)


class DurableStore(Protocol):
    """Database-backed record (unique (key_id, idempotency_key)) so a Redis flush cannot cause a
    double execution. Implemented by each service against its own inbound_request/claim tables."""

    async def get(self, key: str) -> StoredResponse | None: ...
    async def put(self, key: str, rec: StoredResponse) -> None: ...


class RedisStore:
    """Hot path in Redis. Keys follow the ACL namespaces: idem:{ns}:{key_id}:{idem_key} and
    lock:{ns}:idem:{key_id}:{idem_key} (ns = 'hosp' | 'ins'). Optional durable fallback."""

    def __init__(
        self,
        redis: Any,
        namespace: str,
        ttl_s: int = 24 * 3600,
        lock_ttl_s: int = 60,
        durable: DurableStore | None = None,
    ) -> None:
        self.r, self.ns, self.ttl, self.lock_ttl, self.durable = (
            redis,
            namespace,
            ttl_s,
            lock_ttl_s,
            durable,
        )

    def _k(self, key: str) -> str:
        return f"idem:{self.ns}:{key}"

    def _l(self, key: str) -> str:
        return f"lock:{self.ns}:idem:{key}"

    async def get(self, key: str) -> StoredResponse | None:
        raw = await self.r.get(self._k(key))
        if raw is not None:
            d = json.loads(raw)
            return StoredResponse(d["h"], d["s"], base64.b64decode(d["b"]), d["hd"])
        if self.durable is not None:
            return await self.durable.get(key)
        return None

    async def put(self, key: str, rec: StoredResponse) -> None:
        payload = {
            "h": rec.request_hash,
            "s": rec.status,
            "hd": rec.headers,
            "b": base64.b64encode(rec.body).decode(),
        }
        await self.r.set(self._k(key), json.dumps(payload), ex=self.ttl)
        if self.durable is not None:
            await self.durable.put(key, rec)

    async def acquire_lock(self, key: str) -> bool:
        return bool(await self.r.set(self._l(key), "1", nx=True, ex=self.lock_ttl))

    async def release_lock(self, key: str) -> None:
        await self.r.delete(self._l(key))
