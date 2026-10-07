"""Audit explorer: read a case's hash-chained events and verify the chain (officers and admins)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.auth.deps import require_role
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit

router = APIRouter(tags=["audit"])
Auditor = Depends(require_role("officer", "admin"))


def _case(case_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(case_id)
    except ValueError:
        raise ApiError("not_found", "unknown case") from None


@router.get("/v1/audit/{case_id}", operation_id="listAudit")
async def list_audit(
    case_id: str,
    after: int = Query(0, ge=0, description="return events with seq greater than this"),
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = None,
    _: Principal = Auditor,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    cid = _case(case_id)
    s = uow.session
    if not (await s.execute(text("SELECT 1 FROM claim_case WHERE id=:c"), {"c": cid})).first():
        raise ApiError("not_found", "unknown case")
    rows = (
        await s.execute(
            text(
                "SELECT seq, ts, actor_type::text AS actor_type, actor_id, event_type, payload, hash, prev_hash FROM audit_event "
                "WHERE case_id=:c AND seq > :a AND (CAST(:t AS text) IS NULL OR event_type = :t) ORDER BY seq LIMIT :n"
            ),
            {"c": cid, "a": after, "t": event_type, "n": limit + 1},
        )
    ).all()
    items = [
        {
            "seq": r.seq,
            "ts": r.ts.isoformat(),
            "actor_type": r.actor_type,
            "actor_id": r.actor_id,
            "event_type": r.event_type,
            "payload": r.payload,  # redacted when it was written (contract audit.redact)
            "hash": r.hash,
            "prev_hash": r.prev_hash,
        }
        for r in rows[:limit]
    ]
    return {"items": items, "next_after": items[-1]["seq"] if len(rows) > limit and items else None}


@router.post("/v1/audit/{case_id}/verify", operation_id="verifyAudit")
async def verify_audit(
    case_id: str, _: Principal = Auditor, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    cid = _case(case_id)
    if not (
        await uow.session.execute(text("SELECT 1 FROM claim_case WHERE id=:c"), {"c": cid})
    ).first():
        raise ApiError("not_found", "unknown case")
    r = await audit.verify(uow.session, cid)
    return {"ok": r.ok, "checked": r.count, "broken_seq": r.broken_seq, "reason": r.reason}
