"""Event bus (doc 07 §5.6): every state change publishes one event; SSE is a filtered, authorised view of it.
Redis stream = replay buffer (Last-Event-ID resume); Redis pub/sub = live fan-out. Events carry no PII: ids,
statuses and counts only. Key and channel names follow the Redis ACL namespace `sse:hospital:*`."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

STREAM = "sse:hospital:stream"
CHANNEL = "sse:hospital:events"
TICKET = "sse:hospital:ticket:"
RESET = {"type": "reset", "case_id": None, "data": {}}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RedisHub:
    def __init__(self, redis: Any, sessionmaker: Any = None, maxlen: int = 10_000) -> None:
        self.r, self.sm, self.maxlen = redis, sessionmaker, maxlen
        self._assign_cache: dict[str, tuple[float, str | None, bool]] = {}

    async def publish(self, type: str, case_id: str | None, data: dict[str, Any]) -> None:  # noqa: A002
        ev = {"type": type, "case_id": case_id, "ts": _now(), "data": data}
        eid = await self.r.xadd(STREAM, {"e": json.dumps(ev)}, maxlen=self.maxlen, approximate=True)
        ev["id"] = eid
        await self.r.publish(CHANNEL, json.dumps(ev))

    # ---- authorisation -----------------------------------------------------------------------------
    async def _case_assignee(self, case_id: str) -> tuple[str | None, bool]:
        hit = self._assign_cache.get(case_id)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]
        from sqlalchemy import text

        async with self.sm() as s:
            row = (
                await s.execute(
                    text("SELECT assigned_to FROM claim_case WHERE id=:i"), {"i": case_id}
                )
            ).first()
        res = (str(row.assigned_to) if row and row.assigned_to else None, row is not None)
        self._assign_cache[case_id] = (time.monotonic() + 10, *res)
        return res

    async def allowed(self, user: Any, ev: dict[str, Any], scope: str, case_id: str | None) -> bool:
        cid = ev.get("case_id")
        if cid is None:  # system-wide events
            return str(ev.get("type", "")).startswith(("system.", "reset"))
        if scope == "case" and cid != case_id:
            return False
        if "admin" in user.roles and not (user.roles & {"officer", "desk"}):
            return False  # admins see system events only, never case data
        if "officer" in user.roles:
            return True
        assignee, exists = await self._case_assignee(cid)
        return exists and (assignee is None or assignee == str(user.id))

    # ---- reading ---------------------------------------------------------------------------------------
    async def max_trimmed_id(self) -> str | None:
        try:
            info = await self.r.xinfo_stream(STREAM)
        except Exception:  # noqa: BLE001  stream not created yet
            return None
        return info.get("max-deleted-entry-id") if isinstance(info, dict) else None

    async def history(self, since: str) -> list[dict[str, Any]]:
        rows = await self.r.xrange(STREAM, min=f"({since}", max="+")
        out = []
        for eid, fields in rows:
            ev = json.loads(fields["e"])
            ev["id"] = eid
            out.append(ev)
        return out

    async def live(self, queue: asyncio.Queue[dict[str, Any]], stop: asyncio.Event) -> None:
        ps = self.r.pubsub()
        await ps.subscribe(CHANNEL)
        try:
            while not stop.is_set():
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                try:
                    queue.put_nowait(json.loads(msg["data"]))
                except asyncio.QueueFull:
                    queue.put_nowait({"type": "__overflow__"}) if False else stop.set()
        finally:
            await ps.unsubscribe(CHANNEL)
            await ps.aclose()


def _newer(a: str, b: str) -> bool:
    """True if stream id a is newer than b."""
    pa, pb = (int(x) for x in a.split("-")), (int(x) for x in b.split("-"))
    return tuple(pa) > tuple(pb)


async def stream_events(
    hub: RedisHub,
    user: Any,
    scope: str,
    case_id: str | None,
    since: str | None,
    heartbeat_s: float,
    kill: asyncio.Event,
    queue_max: int = 1000,
) -> AsyncIterator[str]:
    """SSE text frames: resume from `since`, then live events, with heartbeat comments."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max)
    stop = asyncio.Event()
    last_sent = since
    task = asyncio.create_task(
        hub.live(queue, stop)
    )  # subscribe first so nothing is missed during replay
    try:
        yield ": connected\n\n"
        if since:
            trimmed = await hub.max_trimmed_id()
            if trimmed and trimmed != "0-0" and not _newer(since, trimmed) and since != trimmed:
                yield 'id: reset\nevent: reset\ndata: {"type":"reset","data":{}}\n\n'  # replay window lost: client refetches
            else:
                for ev in await hub.history(since):
                    if await hub.allowed(user, ev, scope, case_id):
                        last_sent = ev["id"]
                        yield _frame(ev)
                    else:
                        last_sent = ev["id"]
        while not kill.is_set() and not stop.is_set():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if last_sent and "id" in ev and not _newer(ev["id"], last_sent):
                continue  # already replayed
            if await hub.allowed(user, ev, scope, case_id):
                yield _frame(ev)
            if "id" in ev:
                last_sent = ev["id"]
        if stop.is_set() and not kill.is_set():  # slow client: its queue overflowed
            yield 'event: reset\ndata: {"type":"reset","data":{}}\n\n'
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _frame(ev: dict[str, Any]) -> str:
    body = json.dumps({k: ev[k] for k in ("type", "case_id", "ts", "data") if k in ev})
    return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {body}\n\n"
