"""Outbox sender logic (01-01 section 9.3-9.4). Storage and HTTP are injected so each side
can back it with its own SQLAlchemy table; DDL for the table lives in each side's migrations."""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from claim_contract import signing

BACKOFF = [1, 4, 16, 60, 300, 900, 1800, 3600]  # seconds
MAX_ATTEMPTS = 8


@dataclass
class OutboxMessage:
    id: str
    claim_ref: str
    endpoint: str
    body: dict[str, Any]
    idempotency_key: str
    method: str = "POST"
    sequence: int | None = None
    status: str = "pending"  # pending|sending|delivered|dead
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None


@dataclass
class HttpResult:
    status: int
    code: str | None = None  # problem+json "code"
    retry_after: float | None = None


class OutboxStore(Protocol):
    async def fetch_due(self, limit: int) -> list[OutboxMessage]: ...
    async def save(self, m: OutboxMessage) -> None: ...


Transport = Callable[[str, str, bytes, dict[str, str]], Awaitable[HttpResult]]


def canonical_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _reschedule(m: OutboxMessage, err: str, retry_after: float | None, now: datetime) -> None:
    m.attempts += 1
    m.last_error = err
    if m.attempts >= MAX_ATTEMPTS:
        m.status = "dead"
        return
    delay = (
        retry_after
        if retry_after is not None
        else BACKOFF[m.attempts - 1] * random.uniform(0.8, 1.2)  # noqa: S311
    )
    m.status = "pending"
    m.next_attempt_at = now + timedelta(seconds=delay)


async def run_once(
    store: OutboxStore,
    transport: Transport,
    secret: bytes,
    key_id: str,
    contract_version: str = "1.1",
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, int]:
    stats = {"delivered": 0, "retried": 0, "dead": 0}
    for m in await store.fetch_due(limit):
        now_ = now or datetime.now(UTC)
        body = canonical_bytes(m.body)
        ts = signing.now_ts()
        headers = {
            "X-Contract-Version": contract_version,
            "X-Key-Id": key_id,
            "X-Timestamp": ts,
            "X-Idempotency-Key": m.idempotency_key,
            "X-Signature": signing.sign(secret, m.method, m.endpoint, ts, m.idempotency_key, body),
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            r = await transport(m.method, m.endpoint, body, headers)
        except (TimeoutError, ConnectionError, OSError) as e:
            _reschedule(m, str(e) or type(e).__name__, None, now_)
        else:
            if 200 <= r.status < 300:
                m.status = "delivered"
            elif r.status == 401 and r.code == "stale_request":
                m.next_attempt_at = now_  # re-sign immediately, no attempt penalty
            elif r.status in (429, 503) or r.status >= 500 or r.code == "idempotency_in_progress":
                _reschedule(m, str(r.status), r.retry_after, now_)
            else:
                m.status, m.last_error = "dead", f"{r.status} {r.code}"
        await store.save(m)
        key = {"delivered": "delivered", "dead": "dead"}.get(m.status, "retried")
        stats[key] += 1
    return stats
