"""Internal endpoints for n8n / service accounts (03-03 §4.2, 09 §4.1). Role ``n8n-service`` only."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import ProblemError
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ..db import sessionmaker, transaction
from ..models.core import ClaimCase
from ..security.auth import Principal, require_internal
from ..services import audit, cases, context, docs_fetch, events, orchestrator, verification
from ..verification.schemas import RunStart, StepResultBody

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal)])


def _cfg(request: Request) -> Any:
    return request.app.state.config


@router.post("/cases/{case_id}/runs")
async def create_run(case_id: UUID, body: RunStart, request: Request, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    return await verification.start_run(_cfg(request), case_id, body, actor=p.sub)


@router.get("/cases/{case_id}/docs-status")
async def docs_status(case_id: UUID) -> dict[str, Any]:
    async with sessionmaker()() as s:
        return await docs_fetch.docs_status(s, case_id)


@router.get("/cases/{case_id}/context")
async def case_context(case_id: UUID, request: Request, for_: str = Query("all", alias="for")) -> dict[str, Any]:
    """PII-minimised bundle for crews. ``?for=<step>`` selects the shape (identity | coverage | authenticity ...)."""
    async with sessionmaker()() as s:
        case = await s.get(ClaimCase, case_id)
        if case is None:
            raise ProblemError("unknown_claim", "case not found", status=404)
        v = await context.build_vctx(s, _cfg(request), case)
    return context.minimised_context(v, for_)


@router.post("/runs/{run_id}/steps/{step}/start")
async def step_start(run_id: UUID, step: str) -> dict[str, Any]:
    return await verification.mark_step_running(run_id, step)


@router.post("/runs/{run_id}/steps/{step}/result")
async def step_result(run_id: UUID, step: str, request: Request, body: StepResultBody | None = None, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    return await verification.record_step(_cfg(request), run_id, step, body, actor=p.sub)


@router.post("/runs/{run_id}/steps/{step}/evaluate")
async def step_evaluate(run_id: UUID, step: str, request: Request, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    """Pure-rule steps: the API computes everything itself; n8n posts no body."""
    return await verification.record_step(_cfg(request), run_id, step, None, actor=p.sub)


@router.post("/runs/{run_id}/finalize")
async def finalize(run_id: UUID, request: Request, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    return await verification.finalize_run(_cfg(request), run_id, actor=p.sub)


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: InsurerCaseStatus
    reason: str = ""


@router.post("/cases/{case_id}/transition")
async def transition(case_id: UUID, body: TransitionBody, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    async with transaction() as tx:
        case = await cases.get_for_update(tx.session, case_id)
        await cases.transition(tx, case, body.to, actor_id=p.sub, reason=body.reason)
        return cases.case_summary(case)


class AlertBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    severity: str = "medium"
    workflow: str | None = None
    message: str = ""


@router.post("/alerts")
async def alert(body: AlertBody) -> dict[str, Any]:
    await events.publish("delivery.dead_letter" if body.severity == "high" else "case.status_changed", None, {"alert": body.message[:200], "workflow": body.workflow, "severity": body.severity}, ["admin"])
    return {"accepted": True}


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    case_id: UUID | None = None
    data: dict[str, Any] = {}


@router.post("/events")
async def emit_event(body: EventBody) -> dict[str, Any]:
    eid = await events.publish(body.type, body.case_id, body.data)
    return {"event_id": eid}


@router.post("/outbox/kick")
async def outbox_kick() -> dict[str, Any]:
    return {"n8n_triggers_retried": await orchestrator.kick_n8n_triggers()}


@router.post("/audit/verify-all")
async def audit_verify_all() -> dict[str, Any]:
    async with sessionmaker()() as s:
        ids = [r[0] for r in (await s.execute(text("SELECT DISTINCT case_id FROM audit.audit_event WHERE ts > now() - interval '24 hours'"))).all()]
        broken = []
        for cid in ids:
            res = await audit.verify(s, cid)
            if not res.ok:
                broken.append({"case_id": str(cid), "reason": getattr(res, "reason", "")})
    from .. import metrics

    if broken:
        metrics.AUDIT_VERIFY_FAILURES.labels("insurer").inc(len(broken))
    for b in broken:
        await events.publish("delivery.dead_letter", b["case_id"], {"audit_chain_broken": b["reason"]}, ["admin"])
    return {"checked": len(ids), "broken": broken}


@router.post("/housekeeping/idempotency-purge")
async def purge_idempotency() -> dict[str, Any]:
    async with transaction() as tx:
        r = await tx.session.execute(text("DELETE FROM ops.idempotency_record WHERE created_at < now() - interval '7 days'"))
        o = await tx.session.execute(text("DELETE FROM ops.outbox WHERE status = 'sent' AND sent_at < now() - interval '30 days'"))
    return {"idempotency_deleted": r.rowcount, "outbox_deleted": o.rowcount}  # type: ignore[attr-defined]
