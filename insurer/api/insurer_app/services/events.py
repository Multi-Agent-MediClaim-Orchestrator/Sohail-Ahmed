"""Insurer event stream producers/consumers (03-04 §4.4). Redis stream ``ins:events`` in production; an in-process bus
is used in tests/dev when Redis is unavailable. Payloads are redacted and never carry PII."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from claim_contract.insurer_side.audit import redact

from .. import clock


@dataclass
class Event:
    event_id: str
    type: str
    ts: str
    case_id: str | None
    insurer_claim_no: str | None
    payload: dict[str, Any]
    audience: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"event_id": self.event_id, "type": self.type, "ts": self.ts, "case_id": self.case_id,
                           "insurer_claim_no": self.insurer_claim_no, "payload": self.payload, "audience": self.audience})

    @classmethod
    def from_json(cls, raw: str | bytes, event_id: str | None = None) -> Event:
        d = json.loads(raw)
        return cls(event_id or d["event_id"], d["type"], d["ts"], d.get("case_id"), d.get("insurer_claim_no"), d["payload"], d.get("audience", []))


# audience helpers: roles that may see each event (reviewers see cases/queries, approvers add approvals, ...)
TOPIC_OF = {
    "case.": "cases", "verification.": "cases", "decision.": "cases", "query.": "queries", "approval.": "approvals",
    "escalation.": "escalations", "settlement.": "settlement", "delivery.": "admin", "config.": "admin", "sla.": "cases",
}


def topic_of(event_type: str) -> str:
    for prefix, topic in TOPIC_OF.items():
        if event_type.startswith(prefix):
            return topic
    return "cases"


class EventBus(Protocol):
    async def publish(self, ev: Event) -> str: ...
    async def read(self, after_id: str | None, limit: int = 100) -> list[Event]: ...
    async def wait(self, after_id: str | None, timeout: float) -> list[Event]: ...
    async def oldest_id(self) -> str | None: ...


def _id_key(eid: str) -> tuple[int, int]:
    a, _, b = eid.partition("-")
    return int(a), int(b or 0)


class MemoryEventBus:
    def __init__(self, maxlen: int = 10_000) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._seq = 0
        self._cond = asyncio.Condition()

    async def publish(self, ev: Event) -> str:
        async with self._cond:
            self._seq += 1
            ev.event_id = f"{int(time.time() * 1000)}-{self._seq}"
            self._events.append(ev)
            self._cond.notify_all()
        return ev.event_id

    async def read(self, after_id: str | None, limit: int = 100) -> list[Event]:
        out = [e for e in self._events if after_id is None or _id_key(e.event_id) > _id_key(after_id)]
        return out[:limit]

    async def wait(self, after_id: str | None, timeout: float) -> list[Event]:
        got = await self.read(after_id)
        if got:
            return got
        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait(), timeout)
        except TimeoutError:
            return []
        return await self.read(after_id)

    async def oldest_id(self) -> str | None:
        return self._events[0].event_id if self._events else None


class RedisEventBus:
    def __init__(self, redis: Any, stream: str = "ins:events", maxlen: int = 10_000) -> None:
        self.r, self.stream, self.maxlen = redis, stream, maxlen

    async def publish(self, ev: Event) -> str:
        eid = await self.r.xadd(self.stream, {"data": ev.to_json()}, maxlen=self.maxlen, approximate=True)
        return eid.decode() if isinstance(eid, bytes) else str(eid)

    @staticmethod
    def _decode(rows: list[Any]) -> list[Event]:
        out = []
        for eid, fields in rows:
            eid = eid.decode() if isinstance(eid, bytes) else eid
            raw = fields.get(b"data") or fields.get("data")
            out.append(Event.from_json(raw, eid))
        return out

    async def read(self, after_id: str | None, limit: int = 100) -> list[Event]:
        start = f"({after_id}" if after_id else "-"
        return self._decode(await self.r.xrange(self.stream, min=start, max="+", count=limit))

    async def wait(self, after_id: str | None, timeout: float) -> list[Event]:
        last = after_id or "$"
        res = await self.r.xread({self.stream: last}, block=int(timeout * 1000), count=100)
        if not res:
            return []
        return self._decode(res[0][1])

    async def oldest_id(self) -> str | None:
        rows = await self.r.xrange(self.stream, count=1)
        if not rows:
            return None
        eid = rows[0][0]
        return eid.decode() if isinstance(eid, bytes) else str(eid)


_bus: EventBus = MemoryEventBus()


def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def get_bus() -> EventBus:
    return _bus


async def publish(
    event_type: str,
    case_id: UUID | str | None,
    payload: dict[str, Any] | None = None,
    audience: list[str] | None = None,
    insurer_claim_no: str | None = None,
) -> str:
    """Publish to the stream; never raises (a broken event stream must not break a business transaction)."""
    ev = Event("", event_type, clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"), str(case_id) if case_id else None, insurer_claim_no,
               redact(payload or {}), audience or ["reviewer", "senior_reviewer", "approver", "admin"])
    try:
        return await _bus.publish(ev)
    except Exception:  # pragma: no cover
        return ""
