"""Idempotency (01-01 section 5). Pluggable store: in-memory for tests, Redis+DB in services."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

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
    k = f"idem:{key_id}:{idem_key}"
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
