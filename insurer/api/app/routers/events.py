"""SSE event stream (03-04 §4.4): audience filtering per event, Last-Event-ID replay with ``resync``, heartbeat, per-user cap."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from claim_contract.errors import ProblemError
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from ..security.auth import Principal, require_roles
from ..services import events
from ..settings import get_settings

router = APIRouter(tags=["events"])
_conns: dict[str, int] = defaultdict(int)

ROLE_TOPICS = {
    "reviewer": {"cases", "queries", "settlement"},
    "approver": {"cases", "queries", "approvals", "settlement"},
    "senior_reviewer": {"cases", "queries", "approvals", "escalations", "settlement"},
    "admin": {"cases", "queries", "approvals", "escalations", "settlement", "admin"},
}


def visible_topics(p: Principal) -> set[str]:
    out: set[str] = set()
    for r in p.roles:
        out |= ROLE_TOPICS.get(r, set())
    return out


def allowed(ev: events.Event, p: Principal, topics: set[str], case_id: str | None) -> bool:
    if case_id and ev.case_id != case_id:
        return False
    topic = events.topic_of(ev.type)
    if topic not in topics or topic not in visible_topics(p):
        return False
    return bool(set(ev.audience) & set(p.roles)) or "admin" in p.roles


def frame(ev: events.Event) -> str:
    return f"id: {ev.event_id}\nevent: {ev.type}\ndata: {ev.to_json()}\n\n"


async def stream(p: Principal, topics: set[str], case_id: str | None, last_id: str | None) -> AsyncIterator[str]:
    s = get_settings()
    bus = events.get_bus()
    yield "retry: 3000\n\n"
    cursor = last_id
    if last_id:
        oldest = await bus.oldest_id()
        if oldest and events._id_key(last_id) < events._id_key(oldest):
            yield "event: resync\ndata: {}\n\n"  # missed events fell out of the replay window: client refetches
        for ev in (await bus.read(last_id, s.sse_replay_events)):
            cursor = ev.event_id
            if allowed(ev, p, topics, case_id):
                yield frame(ev)
    else:
        existing = await bus.read(None, 100_000)
        cursor = existing[-1].event_id if existing else None  # live only
    try:
        while True:
            batch = await bus.wait(cursor, s.sse_heartbeat_seconds)
            if not batch:
                yield ": heartbeat\n\n"
                continue
            for ev in batch:
                cursor = ev.event_id
                if allowed(ev, p, topics, case_id):
                    yield frame(ev)
    except asyncio.CancelledError:
        raise
    finally:
        _conns[p.sub] -= 1


@router.get("/v1/events/stream")
async def event_stream(topics: str | None = Query(default=None), case_id: str | None = None, last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                       p: Principal = Depends(require_roles("reviewer", "senior_reviewer", "approver", "admin"))) -> StreamingResponse:
    s = get_settings()
    if _conns[p.sub] >= s.sse_max_connections_per_user:
        raise ProblemError("too_many_streams", "too many open event streams for this user", status=429)
    wanted = set(topics.split(",")) if topics else visible_topics(p)
    _conns[p.sub] += 1
    return StreamingResponse(stream(p, wanted, case_id, last_event_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def reset_connections() -> None:  # tests
    _conns.clear()


_: Any = None
