"""Query loop + escalation endpoints (03-05 §4): reviewer, hospital (HMAC) and internal (n8n)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from claim_contract.errors import FieldError, ProblemError
from claim_contract.models import QueryResponse as ContractResponse
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from ..db import sessionmaker
from ..models.core import ClaimCase, Escalation
from ..models.core import Query as QueryRow
from ..models.core import QueryResponse as QRespRow
from ..security.auth import Principal, require_internal, require_roles
from ..services import claims, queries

router = APIRouter(tags=["queries"])
staff = require_roles("reviewer", "senior_reviewer", "approver", "admin")
sender = require_roles("reviewer", "senior_reviewer")


def _q_json(q: QueryRow, resp: QRespRow | None = None) -> dict[str, Any]:
    return {"id": str(q.id), "round": q.round, "category": q.category, "text": q.text, "requested_doc_types": list(q.requested_doc_types or []), "status": q.status,
            "origin": q.origin, "draft_source": q.draft_source, "citations": q.draft_citations, "lint_errors": q.lint_errors, "raised_by": q.raised_by, "due_by": q.due_by.isoformat(),
            "sent_at": q.sent_at.isoformat() if q.sent_at else None, "answered_at": q.answered_at.isoformat() if q.answered_at else None, "acked_at": q.acked_at.isoformat() if q.acked_at else None,
            "is_extension": q.is_extension, "reminder_count": q.reminder_count, "finding_keys": list(q.finding_keys or []),
            "response": {"answer_text": resp.answer_text, "attached_doc_ids": [str(d) for d in resp.attached_doc_ids or []], "responded_by": resp.responded_by,
                         "triage": resp.triage, "triage_source": resp.triage_source} if resp else None}


# ------------------------------------------------------------------ reviewer
@router.get("/v1/cases/{case_id}/queries")
async def list_case_queries(case_id: UUID, response: Response, p: Principal = Depends(staff)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        if (await s.get(ClaimCase, case_id)) is None:
            raise ProblemError("unknown_claim", "case not found", status=404)
        qs = (await s.execute(select(QueryRow).where(QueryRow.case_id == case_id).order_by(QueryRow.round, QueryRow.created_at))).scalars().all()
        out = []
        for q in qs:
            resp = (await s.execute(select(QRespRow).where(QRespRow.query_id == q.id).order_by(QRespRow.received_at.desc()))).scalars().first()
            out.append(_q_json(q, resp))
    return {"items": out}


@router.post("/v1/cases/{case_id}/queries/draft", status_code=202)
async def draft(case_id: UUID, p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.redraft(case_id, p)


class NewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = "other"
    text: str
    requested_doc_types: list[str] = []
    send: bool = False


@router.post("/v1/cases/{case_id}/queries", status_code=201)
async def human_authored(case_id: UUID, body: NewQuery, p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.human_query(case_id, p, body.category, body.text, body.requested_doc_types, body.send)


class PatchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    requested_doc_types: list[str] | None = None
    due_by: datetime | None = None


@router.patch("/v1/queries/{qid}")
async def patch_query(qid: UUID, body: PatchQuery, if_match: str | None = Header(default=None), p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.edit_query(qid, p, body.model_dump(exclude_none=True), if_match)


@router.post("/v1/queries/{qid}/send")
async def send(qid: UUID, p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.send_by_id(qid, p)


class CloseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/v1/queries/{qid}/close")
async def close(qid: UUID, body: CloseBody, p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.close_query(qid, p, body.reason)


class TriageOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    note: str


@router.post("/v1/queries/{qid}/triage/override")
async def triage_override(qid: UUID, body: TriageOverride, p: Principal = Depends(sender)) -> dict[str, Any]:
    return await queries.override_triage(qid, p, body.verdict, body.note)


# ------------------------------------------------------------------ escalations (senior)
senior = require_roles("senior_reviewer", "admin")


@router.get("/v1/escalations")
async def list_escalations(status: str = Query(default="open"), p: Principal = Depends(senior)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        rows = (await s.execute(select(Escalation, ClaimCase.insurer_claim_no).join(ClaimCase, ClaimCase.id == Escalation.case_id).where(Escalation.status == status).order_by(Escalation.opened_at))).all()
    return {"items": [{"id": str(e.id), "case_id": str(e.case_id), "insurer_claim_no": no, "reason": e.reason, "opened_at": e.opened_at.isoformat(),
                       "unresolved": len(e.pack.get("unresolved_findings", []))} for e, no in rows]}


@router.get("/v1/cases/{case_id}/escalation")
async def get_escalation(case_id: UUID, p: Principal = Depends(senior)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        e = (await s.execute(select(Escalation).where(Escalation.case_id == case_id).order_by(Escalation.opened_at.desc()).limit(1))).scalar_one_or_none()
    if e is None:
        raise ProblemError("not_found", "no escalation for this case", status=404)
    return {"id": str(e.id), "status": e.status, "reason": e.reason, "pack": e.pack, "action": e.action}


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    note: str
    query: dict[str, Any] | None = None


@router.post("/v1/cases/{case_id}/escalation/resolve")
async def resolve(case_id: UUID, body: ResolveBody, p: Principal = Depends(senior)) -> dict[str, Any]:
    return await queries.resolve_escalation(case_id, p, body.action, body.note, body.query)


# ------------------------------------------------------------------ hospital (HMAC)
@router.post("/v1/hospital-api/queries/{query_id}/responses", status_code=202)
async def hospital_response(query_id: UUID, request: Request) -> dict[str, Any]:
    ctx = request.state.contract_auth
    try:
        resp = ContractResponse.model_validate_json(request.state.raw_body)
    except ValidationError as exc:
        raise ProblemError("validation_error", "request body failed validation", errors=[FieldError(field=".".join(str(x) for x in e["loc"]), message=e["msg"]) for e in exc.errors()]) from exc
    if resp.query_id != query_id:
        raise ProblemError("validation_error", "query_id in the body does not match the path", status=422)
    async with sessionmaker()() as s:
        h = await claims.hospital_for_key(s, ctx.key_id)
    return await queries.receive_response(h, query_id, resp, uuid.UUID(ctx.idempotency_key))


# ------------------------------------------------------------------ internal (n8n)
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal)])


@internal.get("/queries/due")
async def due(within: str = "PT10M") -> list[dict[str, Any]]:
    minutes = int(within.removeprefix("PT").removesuffix("M") or 10) if within.startswith("PT") and within.endswith("M") else 10
    return await queries.due_items(minutes)


@internal.post("/queries/{qid}/reminder")
async def post_reminder(qid: UUID) -> dict[str, Any]:
    return await queries.reminder(qid)


class TimeoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round: int


@internal.post("/cases/{case_id}/round-timeout")
async def round_timeout(case_id: UUID, body: TimeoutBody) -> dict[str, Any]:
    return await queries.on_timeout(case_id, body.round)


class TriageResultBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    verdict: str = "partial"
    resolved_finding_keys: list[str] = []
    remaining_finding_keys: list[str] = []
    notes: str = ""


@internal.post("/queries/{qid}/triage-result")
async def triage_result(qid: UUID, body: TriageResultBody) -> dict[str, Any]:
    """n8n posts the crew's triage; the API re-applies its own rules (an agent cannot resolve what code still sees open)."""
    from ..services import jobs

    await jobs.enqueue("triage_response", query_id=str(qid))
    return {"accepted": True}
