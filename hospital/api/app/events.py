"""SSE hub. Doc 07 replaces this in-memory publisher with Redis streams; the interface stays."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass
class Event:
    type: str
    case_id: str | None
    data: dict[str, Any]
    id: int = 0
    ts: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


class Hub(Protocol):
    async def publish(self, type: str, case_id: str | None, data: dict[str, Any]) -> None: ...  # noqa: A002


class InMemoryHub:
    """Ordered per-hub event log with live subscribers; good for tests and a single process."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._subs: dict[int, asyncio.Queue[Event]] = {}
        self._next = 0

    async def publish(self, type: str, case_id: str | None, data: dict[str, Any]) -> None:  # noqa: A002
        self._next += 1
        ev = Event(type, case_id, data, id=self._next)
        self.events.append(ev)
        for q in self._subs.values():
            q.put_nowait(ev)

    def subscribe(self) -> tuple[int, asyncio.Queue[Event]]:
        sid = len(self._subs) + 1000 + self._next
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        self._subs.pop(sid, None)

    def of_type(self, prefix: str) -> list[Event]:
        return [e for e in self.events if e.type.startswith(prefix)]


class CompletenessHook(Protocol):
    async def __call__(self, case_id: str, reason: str) -> None: ...


async def noop_completeness(case_id: str, reason: str) -> None:  # replaced by doc 04
    return None
