"""At-least-once delivery with backoff and dead-lettering (01-01 §9.3-9.4).

``OutboxSender`` is storage agnostic: it talks to an ``OutboxRepo``. The hospital uses the SQL repo for the
contract table (``SqlOutboxRepo`` / ``OutboxMessage``); the insurer adapts ``ops.outbox`` (03-02 §6.3)."""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from claim_contract import signing

BACKOFF = [1, 4, 16, 60, 300, 900, 1800, 3600]  # seconds; max 8 attempts
MAX_ATTEMPTS = 8


@dataclass
class OutboxItem:
    id: Any
    endpoint: str
    body: dict[str, Any]
    idempotency_key: str
    method: str = "POST"
    claim_ref: str | None = None
    sequence: int | None = None
    attempts: int = 0
    base_url: str | None = None  # per-item override (insurer: per-hospital callback URL)
    journey_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class OutboxRepo(Protocol):
    async def fetch_due(self, limit: int) -> list[OutboxItem]: ...
    async def mark_delivered(self, item: OutboxItem) -> None: ...
    async def reschedule(self, item: OutboxItem, next_attempt_at: datetime, error: str) -> None: ...
    async def mark_dead(self, item: OutboxItem, error: str) -> None: ...


def canonical_bytes(body: Any) -> bytes:
    """Deterministic request body bytes: sign exactly what is sent."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )


def jitter(lo: float = 0.8, hi: float = 1.2) -> float:
    return random.uniform(lo, hi)


def problem_code(resp: httpx.Response) -> str | None:
    try:
        code = resp.json().get("code")
        return code if isinstance(code, str) else None
    except Exception:
        return None


@dataclass
class SendResult:
    delivered: int = 0
    retried: int = 0
    dead: int = 0


class OutboxSender:
    def __init__(
        self,
        repo: OutboxRepo,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        key_id: str,
        secret: bytes,
        contract_version: str = "1.1",
        backoff_scale: float = 1.0,
        on_dead: Callable[[OutboxItem, str], Awaitable[None]] | None = None,
        on_delivered: Callable[[OutboxItem], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo, self.http = repo, http
        self.base_url, self.key_id, self.secret = base_url.rstrip("/"), key_id, secret
        self.contract_version = contract_version
        self.backoff_scale = backoff_scale
        self.on_dead, self.on_delivered, self.now = on_dead, on_delivered, now

    def _delay(self, attempts: int, retry_after: str | None, immediate: bool) -> float:
        if immediate:
            return 0.0
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return BACKOFF[min(attempts, len(BACKOFF)) - 1] * self.backoff_scale * jitter()

    async def _reschedule(
        self, m: OutboxItem, err: str, *, immediate: bool = False, retry_after: str | None = None
    ) -> bool:
        """Returns True if rescheduled, False if it went dead."""
        m.attempts += 1
        if m.attempts >= MAX_ATTEMPTS:
            await self.repo.mark_dead(m, err)
            if self.on_dead:
                await self.on_dead(m, err)
            return False
        delay = self._delay(m.attempts, retry_after, immediate)
        await self.repo.reschedule(m, self.now() + timedelta(seconds=delay), err)
        return True

    async def send_one(self, m: OutboxItem) -> str:
        """Returns one of delivered | retry | dead."""
        body = canonical_bytes(m.body)
        base = (m.base_url or self.base_url).rstrip("/")
        headers = signing.build_headers(
            self.secret,
            self.key_id,
            m.method,
            m.endpoint,
            body,
            m.idempotency_key,
            contract_version=self.contract_version,
            journey_id=m.journey_id,
            request_id=str(uuid.uuid4()),
            now=self.now(),
        )
        try:
            r = await self.http.request(m.method, base + m.endpoint, content=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return "retry" if await self._reschedule(m, f"{type(exc).__name__}: {exc}") else "dead"
        code = problem_code(r)
        if 200 <= r.status_code < 300:
            await self.repo.mark_delivered(m)
            if self.on_delivered:
                await self.on_delivered(m)
            return "delivered"
        if r.status_code == 401 and code == "stale_request":
            return "retry" if await self._reschedule(m, "stale_request", immediate=True) else "dead"
        if (
            r.status_code in (429, 503)
            or r.status_code >= 500
            or code in ("idempotency_in_progress", "request_in_progress")
        ):
            ok = await self._reschedule(
                m, f"{r.status_code} {code or ''}".strip(), retry_after=r.headers.get("Retry-After")
            )
            return "retry" if ok else "dead"
        await self.repo.mark_dead(m, f"{r.status_code} {code}")  # permanent 4xx
        if self.on_dead:
            await self.on_dead(m, f"{r.status_code} {code}")
        return "dead"

    async def run_once(self, limit: int = 20) -> SendResult:
        res = SendResult()
        for m in await self.repo.fetch_due(limit):
            outcome = await self.send_one(m)
            if outcome == "delivered":
                res.delivered += 1
            elif outcome == "retry":
                res.retried += 1
            else:
                res.dead += 1
        return res


# --------------------------------------------------------------------------------------------------
# SQL implementation of the contract table (hospital side, tests)
# --------------------------------------------------------------------------------------------------
OUTBOX_DDL_PG = """
CREATE TABLE IF NOT EXISTS outbox_message (
  id UUID PRIMARY KEY, claim_ref TEXT NOT NULL, endpoint TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'POST',
  body JSONB NOT NULL, idempotency_key UUID NOT NULL, sequence BIGINT,
  status TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox_message (status, next_attempt_at);
"""


class SqlOutboxRepo:
    """Contract §9.3 table. Works on PostgreSQL (``FOR UPDATE SKIP LOCKED``) and SQLite (tests)."""

    def __init__(self, sessionmaker: Any, table: str = "outbox_message") -> None:
        self.sm, self.table = sessionmaker, table

    async def enqueue(self, session: Any, item: OutboxItem) -> None:
        from sqlalchemy import text

        pg = session.bind.dialect.name == "postgresql"
        cast = "CAST(:body AS JSONB)" if pg else ":body"
        await session.execute(
            text(
                f"INSERT INTO {self.table} (id, claim_ref, endpoint, method, body, idempotency_key, sequence) "
                f"VALUES (:id, :cr, :ep, :m, {cast}, :ik, :seq)"
            ),
            {
                "id": str(item.id),
                "cr": item.claim_ref,
                "ep": item.endpoint,
                "m": item.method,
                "body": json.dumps(item.body),
                "ik": item.idempotency_key,
                "seq": item.sequence,
            },
        )

    async def fetch_due(self, limit: int) -> list[OutboxItem]:
        from sqlalchemy import text

        async with self.sm() as s, s.begin():
            pg = s.bind.dialect.name == "postgresql"
            lock = " FOR UPDATE SKIP LOCKED" if pg else ""
            now = datetime.now(UTC)
            rows = (
                await s.execute(
                    text(
                        f"SELECT id, claim_ref, endpoint, method, body, idempotency_key, sequence, attempts "
                        f"FROM {self.table} WHERE status = 'pending' AND next_attempt_at <= :now "
                        f"ORDER BY next_attempt_at LIMIT :l{lock}"
                    ),
                    {"now": now if pg else now.isoformat(), "l": limit},
                )
            ).all()
            out = []
            for r in rows:
                body = r.body if isinstance(r.body, dict) else json.loads(r.body)
                out.append(
                    OutboxItem(
                        str(r.id),
                        r.endpoint,
                        body,
                        str(r.idempotency_key),
                        r.method,
                        r.claim_ref,
                        r.sequence,
                        r.attempts,
                    )
                )
            return out

    async def _update(self, item: OutboxItem, **cols: Any) -> None:
        from sqlalchemy import text

        sets = ", ".join(f"{k} = :{k}" for k in cols)
        async with self.sm() as s, s.begin():
            await s.execute(
                text(f"UPDATE {self.table} SET {sets} WHERE id = :_id"), {**cols, "_id": item.id}
            )

    async def mark_delivered(self, item: OutboxItem) -> None:
        pg = True
        async with self.sm() as s:
            pg = s.bind.dialect.name == "postgresql"
        now = datetime.now(UTC)
        await self._update(
            item,
            status="delivered",
            delivered_at=now if pg else now.isoformat(),
            attempts=item.attempts,
        )

    async def reschedule(self, item: OutboxItem, next_attempt_at: datetime, error: str) -> None:
        async with self.sm() as s:
            pg = s.bind.dialect.name == "postgresql"
        await self._update(
            item,
            attempts=item.attempts,
            next_attempt_at=next_attempt_at if pg else next_attempt_at.isoformat(),
            last_error=error[:500],
        )

    async def mark_dead(self, item: OutboxItem, error: str) -> None:
        await self._update(item, status="dead", attempts=item.attempts, last_error=error[:500])
