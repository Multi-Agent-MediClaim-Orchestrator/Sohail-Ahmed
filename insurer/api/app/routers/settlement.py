"""Settlement endpoints (03-06 §4). The bank callback is HMAC-authenticated (key id ``bank-sim``) by the contract middleware."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from claim_contract.errors import ProblemError
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from ..db import sessionmaker, transaction
from ..models.core import ClaimCase, Settlement, SettlementEvent, SettlementTask
from ..security.auth import Principal, require_internal, require_roles
from ..services import audit, settlement
from ..settings import get_settings

router = APIRouter(tags=["settlement"])
staff = require_roles("reviewer", "senior_reviewer", "approver", "admin")
senior = require_roles("senior_reviewer", "admin")
admin = require_roles("admin")


def _st(r: Settlement) -> dict[str, Any]:
    return {"id": str(r.id), "case_id": str(r.case_id), "status": r.status, "amount": f"{r.amount:.2f}", "gross_amount": f"{r.gross_amount:.2f}", "adjustments": r.adjustments,
            "payee_type": r.payee_type, "payee_ref": r.payee_ref, "utr": r.utr, "mode": r.mode, "attempt_count": r.attempt_count, "failure_reason": r.failure_reason,
            "initiated_at": r.initiated_at.isoformat() if r.initiated_at else None, "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "next_retry_at": r.next_retry_at.isoformat() if r.next_retry_at else None}


@router.get("/v1/cases/{case_id}/settlement")
async def case_settlement(case_id: UUID, p: Principal = Depends(staff)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        sts = (await s.execute(select(Settlement).where(Settlement.case_id == case_id).order_by(Settlement.created_at.desc()))).scalars().all()
        evs = (await s.execute(select(SettlementEvent).join(Settlement, Settlement.id == SettlementEvent.settlement_id).where(Settlement.case_id == case_id).order_by(SettlementEvent.created_at))).scalars().all()
        tasks = (await s.execute(select(SettlementTask).where(SettlementTask.case_id == case_id))).scalars().all()
    return {"settlements": [_st(x) for x in sts], "events": [{"event": e.event, "at": e.created_at.isoformat(), "detail": e.detail} for e in evs],
            "tasks": [{"id": str(t.id), "kind": t.kind, "status": t.status, "detail": t.detail} for t in tasks]}


@router.get("/v1/settlements")
async def list_settlements(status: str | None = None, date_from: date | None = None, date_to: date | None = None, payee_type: str | None = None,
                           limit: int = Query(default=50, ge=1, le=200), offset: int = 0, p: Principal = Depends(staff)) -> dict[str, Any]:
    where: list[str] = ["1=1"]
    params: dict[str, Any] = {"lim": limit, "off": offset}
    if status:
        where.append("status = :status")
        params["status"] = status
    if payee_type:
        where.append("payee_type = :pt")
        params["pt"] = payee_type
    if date_from:
        where.append("created_at::date >= :df")
        params["df"] = date_from
    if date_to:
        where.append("created_at::date <= :dt")
        params["dt"] = date_to
    async with sessionmaker()() as s:
        rows = (await s.execute(select(Settlement).from_statement(text(f"SELECT * FROM core.settlement WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT :lim OFFSET :off").bindparams(**params)))).scalars().all()
    return {"items": [_st(r) for r in rows], "limit": limit, "offset": offset}


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""
    reason: str = ""


@router.post("/v1/settlements/{sid}/retry")
async def retry(sid: UUID, body: NoteBody, p: Principal = Depends(senior)) -> dict[str, Any]:
    return await settlement.retry(sid, p, body.note)


@router.post("/v1/settlements/{sid}/reverse")
async def reverse(sid: UUID, p: Principal = Depends(senior)) -> dict[str, Any]:
    return await settlement.reverse(sid, p)


@router.post("/v1/settlements/{sid}/release-utilisation")
async def release(sid: UUID, body: NoteBody, p: Principal = Depends(senior)) -> dict[str, Any]:
    return await settlement.release_utilisation(sid, p, body.reason)


@router.get("/v1/settlement-tasks")
async def tasks(status: str = "open", p: Principal = Depends(senior)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        rows = (await s.execute(select(SettlementTask).where(SettlementTask.status == status).order_by(SettlementTask.opened_at))).scalars().all()
    return {"items": [{"id": str(t.id), "kind": t.kind, "status": t.status, "settlement_id": str(t.settlement_id) if t.settlement_id else None, "case_id": str(t.case_id) if t.case_id else None,
                       "detail": t.detail, "opened_at": t.opened_at.isoformat()} for t in rows]}


@router.post("/v1/settlement-tasks/{tid}/close")
async def close_task(tid: UUID, body: NoteBody, p: Principal = Depends(senior)) -> dict[str, Any]:
    async with transaction() as tx:
        t = await tx.session.get(SettlementTask, tid)
        if t is None:
            raise ProblemError("not_found", "task not found", status=404)
        t.status, t.closed_by = "done", p.sub
        await tx.session.execute(text("UPDATE core.settlement_task SET closed_at = now() WHERE id = :i"), {"i": tid})
        return {"id": str(tid), "status": "done"}


@router.get("/v1/admin/settlements/report")
async def report(date_from: date, date_to: date, p: Principal = Depends(admin)) -> Response:
    csv_text = await settlement.report_csv(date_from, date_to, get_settings().report_tz)
    return Response(csv_text, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=settlements_{date_from}_{date_to}.csv"})


@router.get("/v1/admin/settlements/reconciliation")
async def reconciliation(p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        r = (await s.execute(text("SELECT day, kind, diffs, created_at FROM core.reconciliation_run ORDER BY created_at DESC LIMIT 1"))).one_or_none()
    return {"last": {"day": r.day.isoformat(), "kind": r.kind, "diffs": r.diffs, "at": r.created_at.isoformat()} if r else None}


# ------------------------------------------------------------------ internal
@router.post("/internal/cases/{case_id}/settlement/initiate", dependencies=[Depends(require_internal)])
async def initiate(case_id: UUID) -> dict[str, Any]:
    return await settlement.initiate(case_id)


@router.get("/internal/settlement/{case_id}/status", dependencies=[Depends(require_internal)])
async def status(case_id: UUID) -> dict[str, Any]:
    async with sessionmaker()() as s:
        st = (await s.execute(select(Settlement).where(Settlement.case_id == case_id).order_by(Settlement.created_at.desc()).limit(1))).scalar_one_or_none()
        c = await s.get(ClaimCase, case_id)
    return {"status": "settled" if (c and c.status in ("settled", "closed")) else (st.status if st else "none")}


@router.post("/internal/settlement/{case_id}/initiate", dependencies=[Depends(require_internal)])
async def initiate_n8n(case_id: UUID) -> dict[str, Any]:
    return await settlement.initiate(case_id)


@router.post("/internal/settlement/bank-callback")
async def bank_callback(request: Request) -> dict[str, Any]:
    """HMAC (key id ``bank-sim``) verified by the middleware. Replay-safe; amount/UTR anomalies open tasks instead of paying."""
    body = json.loads(request.state.raw_body)
    for k in ("settlement_id", "status"):
        if k not in body:
            raise ProblemError("validation_error", f"missing {k}", status=422)
    return await settlement.on_bank_callback(body)


_ = audit
