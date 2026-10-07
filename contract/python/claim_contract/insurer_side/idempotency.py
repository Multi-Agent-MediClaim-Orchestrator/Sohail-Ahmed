"""Idempotency (01-01 §5): Redis hot path + durable DB path, lock to stop concurrent duplicates.

``request_hash = sha256(method + target + body)`` so a key re-used on a different path or body conflicts.
Only 2xx and 4xx responses are stored — never 5xx (handlers must be safe to re-run)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


def request_hash(method: str, target: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(target.encode())
    h.update(body)
    return h.hexdigest()


@dataclass
class StoredResponse:
    request_hash: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "request_hash": self.request_hash,
                "status": self.status,
                "body": self.body.decode("utf-8", "replace"),
                "headers": self.headers,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> StoredResponse:
        d = json.loads(raw)
        return cls(
            d["request_hash"],
            d["status"],
            d["body"].encode(),
            d.get("headers", {}),
            d.get("created_at", 0.0),
        )


class IdempotencyStore(Protocol):
    async def get(self, key_id: str, idem_key: str) -> StoredResponse | None: ...
    async def put(self, key_id: str, idem_key: str, rec: StoredResponse) -> None: ...
    async def acquire(self, key_id: str, idem_key: str, ttl: int = 60) -> bool: ...
    async def release(self, key_id: str, idem_key: str) -> None: ...


class MemoryIdempotencyStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], StoredResponse] = {}
        self._locks: dict[tuple[str, str], float] = {}

    async def get(self, key_id: str, idem_key: str) -> StoredResponse | None:
        return self._data.get((key_id, idem_key))

    async def put(self, key_id: str, idem_key: str, rec: StoredResponse) -> None:
        self._data[(key_id, idem_key)] = rec

    async def acquire(self, key_id: str, idem_key: str, ttl: int = 60) -> bool:
        k = (key_id, idem_key)
        now = time.monotonic()
        if self._locks.get(k, 0) > now:
            return False
        self._locks[k] = now + ttl
        return True

    async def release(self, key_id: str, idem_key: str) -> None:
        self._locks.pop((key_id, idem_key), None)


class RedisIdempotencyStore:
    """Hot path. ``redis`` is a ``redis.asyncio.Redis`` (or fakeredis). TTL 24 h."""

    def __init__(self, redis: Any, ttl: int = 86400, prefix: str = "idem") -> None:
        self.r, self.ttl, self.prefix = redis, ttl, prefix

    def _k(self, key_id: str, idem_key: str) -> str:
        return f"{self.prefix}:{key_id}:{idem_key}"

    async def get(self, key_id: str, idem_key: str) -> StoredResponse | None:
        raw = await self.r.get(self._k(key_id, idem_key))
        return StoredResponse.from_json(raw) if raw else None

    async def put(self, key_id: str, idem_key: str, rec: StoredResponse) -> None:
        await self.r.set(self._k(key_id, idem_key), rec.to_json(), ex=self.ttl)

    async def acquire(self, key_id: str, idem_key: str, ttl: int = 60) -> bool:
        return bool(await self.r.set(self._k(key_id, idem_key) + ":lock", "1", nx=True, ex=ttl))

    async def release(self, key_id: str, idem_key: str) -> None:
        await self.r.delete(self._k(key_id, idem_key) + ":lock")


class SqlIdempotencyStore:
    """Durable path. ``table`` has columns (key_id, idempotency_key, request_hash, response_status,
    response_body, created_at) — e.g. ``ops.idempotency_record`` on the insurer. Locks are process-local
    (``pg_advisory`` would need a held connection); the Redis lock + the owning table's unique constraint are
    the cross-process guards, which is what the contract requires (T10)."""

    def __init__(self, sessionmaker: Any, table: str = "ops.idempotency_record") -> None:
        self.sm, self.table = sessionmaker, table
        self._mem = MemoryIdempotencyStore()

    async def get(self, key_id: str, idem_key: str) -> StoredResponse | None:
        from sqlalchemy import text

        async with self.sm() as s:
            row = (
                await s.execute(
                    text(
                        f"SELECT request_hash, response_status, response_body FROM {self.table} "
                        "WHERE key_id = :k AND idempotency_key = CAST(:i AS UUID)"
                    ),
                    {"k": key_id, "i": idem_key},
                )
            ).one_or_none()
        if row is None:
            return None
        body = row.response_body
        raw = body if isinstance(body, (str, bytes)) else json.dumps(body)
        data = json.loads(raw) if raw else {}
        # we store {"headers": {...}, "body": "<text>"} so replays are byte-identical
        return StoredResponse(
            row.request_hash,
            int(row.response_status),
            data.get("body", "").encode(),
            data.get("headers", {}),
        )

    async def put(self, key_id: str, idem_key: str, rec: StoredResponse) -> None:
        from sqlalchemy import text

        payload = json.dumps({"headers": rec.headers, "body": rec.body.decode("utf-8", "replace")})
        async with self.sm() as s, s.begin():
            await s.execute(
                text(
                    f"INSERT INTO {self.table} (key_id, idempotency_key, request_hash, response_status, response_body) "
                    "VALUES (:k, CAST(:i AS UUID), :h, :st, CAST(:b AS JSONB)) ON CONFLICT DO NOTHING"
                ),
                {"k": key_id, "i": idem_key, "h": rec.request_hash, "st": rec.status, "b": payload},
            )

    async def acquire(self, key_id: str, idem_key: str, ttl: int = 60) -> bool:
        return await self._mem.acquire(key_id, idem_key, ttl)

    async def release(self, key_id: str, idem_key: str) -> None:
        await self._mem.release(key_id, idem_key)


class ChainedIdempotencyStore:
    """Hot store first, durable store as fallback/backfill. Hot-store failures degrade gracefully (Redis down)."""

    def __init__(
        self,
        hot: IdempotencyStore | None,
        durable: IdempotencyStore | None,
        on_degraded: Callable[[str], None] | None = None,
    ) -> None:
        self.hot, self.durable = hot, durable
        self.on_degraded = on_degraded or (lambda _m: None)
        self._local = MemoryIdempotencyStore()  # lock fallback when Redis is down

    async def get(self, key_id: str, idem_key: str) -> StoredResponse | None:
        if self.hot is not None:
            try:
                rec = await self.hot.get(key_id, idem_key)
                if rec:
                    return rec
            except Exception as exc:
                self.on_degraded(f"hot get failed: {exc!r}")
        if self.durable is not None:
            rec = await self.durable.get(key_id, idem_key)
            if rec and self.hot is not None:
                try:
                    await self.hot.put(key_id, idem_key, rec)
                except Exception:  # pragma: no cover
                    pass
            return rec
        return None

    async def put(self, key_id: str, idem_key: str, rec: StoredResponse) -> None:
        if self.durable is not None:
            await self.durable.put(key_id, idem_key, rec)
        if self.hot is not None:
            try:
                await self.hot.put(key_id, idem_key, rec)
            except Exception as exc:
                self.on_degraded(f"hot put failed: {exc!r}")

    async def acquire(self, key_id: str, idem_key: str, ttl: int = 60) -> bool:
        if self.hot is not None:
            try:
                return await self.hot.acquire(key_id, idem_key, ttl)
            except Exception as exc:
                self.on_degraded(f"hot lock failed: {exc!r}")
        return await self._local.acquire(key_id, idem_key, ttl)

    async def release(self, key_id: str, idem_key: str) -> None:
        if self.hot is not None:
            try:
                await self.hot.release(key_id, idem_key)
            except Exception:  # pragma: no cover
                pass
        await self._local.release(key_id, idem_key)


@dataclass
class IdemOutcome:
    kind: str  # "replay" | "conflict" | "in_progress" | "execute"
    rec: StoredResponse | None = None


async def check(store: IdempotencyStore, key_id: str, idem_key: str, req_hash: str) -> IdemOutcome:
    """Algorithm from 01-01 §5.2 up to (not including) executing the handler."""
    rec = await store.get(key_id, idem_key)
    if rec is not None:
        if rec.request_hash == req_hash:
            return IdemOutcome("replay", rec)
        return IdemOutcome("conflict")
    if not await store.acquire(key_id, idem_key):
        return IdemOutcome("in_progress")
    return IdemOutcome("execute")


async def run_idempotent(
    store: IdempotencyStore,
    key_id: str,
    idem_key: str,
    req_hash: str,
    handler: Callable[[], Awaitable[StoredResponse]],
) -> tuple[StoredResponse, bool]:
    """Convenience wrapper for non-ASGI callers (jobs, tests). Returns (response, replayed)."""
    out = await check(store, key_id, idem_key, req_hash)
    if out.kind == "replay":
        assert out.rec is not None
        return out.rec, True
    if out.kind == "conflict":
        return StoredResponse(req_hash, 409, b'{"code":"idempotency_conflict"}'), False
    if out.kind == "in_progress":
        return StoredResponse(req_hash, 409, b'{"code":"idempotency_in_progress"}'), False
    try:
        rec = await handler()
        if rec.status < 500:
            await store.put(key_id, idem_key, rec)
        return rec, False
    finally:
        await store.release(key_id, idem_key)
