"""SSE endpoints: one-time ticket (EventSource cannot send headers) + the stream itself."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.auth.deps import guard, require_role
from app.auth.principal import Principal
from app.core.errors import ApiError
from app.sse.hub import TICKET, RedisHub, stream_events

router = APIRouter(tags=["stream"])
Human = Depends(require_role("desk", "officer", "admin"))


class TicketReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = "inbox"
    case_id: str | None = None


def _hub(request: Request) -> RedisHub:
    hub = request.app.state.hub
    if not isinstance(hub, RedisHub):
        raise ApiError("service_unavailable", "the event stream is not available")
    return hub


@router.post("/v1/stream/ticket", operation_id="streamTicket")
async def ticket(body: TicketReq, request: Request, p: Principal = Human) -> dict[str, Any]:
    _hub(request)
    if body.scope not in ("inbox", "case"):
        raise ApiError("validation_error", "scope must be inbox or case")
    if body.scope == "case":
        if not body.case_id:
            raise ApiError("validation_error", "case_id is required for scope=case")
        from app.auth.deps import case_scope_sql

        sql, params = case_scope_sql(p)
        try:
            cid = uuid.UUID(body.case_id)
        except ValueError:
            raise ApiError("validation_error", "case_id must be a uuid") from None
        async with request.app.state.sessionmaker() as s:
            row = (
                await s.execute(
                    text(f"SELECT 1 FROM claim_case c WHERE c.id=:i AND {sql}"),  # noqa: S608
                    {"i": cid, **params},
                )
            ).first()  # noqa: S608
        if row is None and "admin" not in p.roles:
            raise ApiError("forbidden", "you cannot follow that case")
    t = "tk_" + secrets.token_urlsafe(24)
    ttl = request.app.state.settings.sse_ticket_ttl_s
    await request.app.state.redis.set(
        TICKET + t,
        json.dumps(
            {
                "uid": str(p.id),
                "scope": body.scope,
                "case_id": body.case_id,
                "roles": sorted(p.roles),
            }
        ),
        ex=ttl,
    )
    return {"ticket": t, "expires_in": ttl}


@guard
async def ticket_holder(request: Request, ticket: str = "") -> dict[str, Any]:
    """The ticket IS the credential on this route: one-time, 30 s, bound to a user and scope."""
    raw = await request.app.state.redis.getdel(TICKET + ticket) if ticket else None
    if not raw:
        raise ApiError(
            "unauthenticated",
            "invalid or expired ticket",
            status=401,
            headers={"WWW-Authenticate": 'Bearer error="invalid_ticket"'},
        )
    return json.loads(raw)  # type: ignore[no-any-return]


@router.get("/v1/stream", operation_id="stream")
async def stream(
    request: Request,
    ticket: str = "",
    scope: str = "inbox",
    case_id: str | None = None,
    last_id: str | None = None,
    t: dict[str, Any] = Depends(ticket_holder),
) -> StreamingResponse:
    st = request.app.state
    hub = _hub(request)
    if t["scope"] == "case" and case_id not in (None, t["case_id"]):
        raise ApiError("forbidden", "the ticket does not cover that case")
    scope, case_id = t["scope"], (t["case_id"] if t["scope"] == "case" else None)
    user = Principal("human", t["uid"], frozenset(t["roles"]), id=uuid.UUID(t["uid"]))
    since = request.headers.get("last-event-id") or last_id
    kill = asyncio.Event()
    conns: list[asyncio.Event] = st.sse_conns.setdefault(t["uid"], [])
    conns.append(kill)
    while len(conns) > st.settings.sse_max_connections:  # close the oldest beyond the cap
        conns.pop(0).set()

    async def gen() -> Any:
        try:
            async for frame in stream_events(
                hub, user, scope, case_id, since, st.settings.sse_heartbeat_s, kill
            ):
                yield frame
        finally:
            if kill in conns:
                conns.remove(kill)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
