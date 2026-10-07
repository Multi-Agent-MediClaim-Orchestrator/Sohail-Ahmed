"""Reviewer-facing case endpoints (03-03 §4.1): list, header, workspace, assign, rerun, override, priority, documents, audit."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from claim_contract.errors import ProblemError
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from ..db import sessionmaker, transaction
from ..models.core import (
    BillLine,
    CalculationResult,
    ClaimCase,
    ClaimDocument,
    Decision,
    FindingOverride,
    VerificationRun,
    VerificationStep,
)
from ..security.auth import Principal, require_roles
from ..services import audit, cases, docs_fetch, events, jobs, verification
from ..settings import get_settings
from ..verification.schemas import STEP_ORDER

STAFF = ("reviewer", "senior_reviewer", "approver", "admin")
router = APIRouter(prefix="/v1", tags=["cases"])
view = require_roles(*STAFF)


def mask_name(name: str) -> str:
    return " ".join(p[0] + "*" * (len(p) - 1) if len(p) > 1 else p for p in name.split())


def _etag(case: ClaimCase) -> str:
    return f'"case-v{case.etag}"'


# ------------------------------------------------------------------ list
@router.get("/cases")
async def list_cases(
    status: list[str] | None = Query(default=None), assignee: str | None = None, hospital: str | None = None, claim_type: str | None = None,
    priority: int | None = None, sla_breach: bool | None = None, q: str | None = None, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None,
    updated_since: datetime | None = None, p: Principal = Depends(view),
) -> dict[str, Any]:
    where: list[str] = ["1=1"]
    params: dict[str, Any] = {"lim": limit + 1}
    if status:
        where.append("status::text = ANY(:status)")
        params["status"] = status
    if assignee:
        where.append("assigned_reviewer = :assignee")
        params["assignee"] = "" if assignee == "me" else assignee
        if assignee == "me":
            params["assignee"] = p.sub
    if hospital:
        where.append("hospital_name ILIKE :hospital")
        params["hospital"] = f"%{hospital}%"
    if claim_type:
        where.append("claim_type = :ct")
        params["ct"] = claim_type
    if priority:
        where.append("priority = :prio")
        params["prio"] = priority
    if sla_breach is not None:
        where.append("sla_breached = :slab")
        params["slab"] = sla_breach
    if q:
        where.append("(insurer_claim_no ILIKE :q OR hospital_claim_ref ILIKE :q OR member_name ILIKE :q)")
        params["q"] = f"%{q}%"
    if updated_since:
        where.append("updated_at > :us")
        params["us"] = updated_since
    if cursor:
        pr, sla, cid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        sla = datetime.fromisoformat(sla)
        where.append("(priority, coalesce(sla_due_at, 'infinity'::timestamptz), id) > (:cp, CAST(:cs AS timestamptz), CAST(:ci AS uuid))")
        params.update(cp=pr, cs=sla, ci=cid)
    sql = ("SELECT id, insurer_claim_no, hospital_claim_ref, hospital_name, claim_type, admission_type, status::text AS status, priority, claimed_amount, recommended_amount, "
           "approved_amount, assigned_reviewer, sla_due_at, sla_breached, received_at, updated_at, etag, member_name FROM core.v_case_list WHERE " + " AND ".join(where) +
           " ORDER BY priority, coalesce(sla_due_at, 'infinity'::timestamptz), id LIMIT :lim")
    async with sessionmaker()() as s:
        rows = (await s.execute(text(sql), params)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"id": str(r.id), "insurer_claim_no": r.insurer_claim_no, "hospital_claim_ref": r.hospital_claim_ref, "hospital_name": r.hospital_name, "claim_type": r.claim_type,
              "admission_type": r.admission_type, "status": r.status, "priority": r.priority, "claimed_amount": f"{r.claimed_amount:.2f}",
              "recommended_amount": f"{r.recommended_amount:.2f}" if r.recommended_amount is not None else None, "approved_amount": f"{r.approved_amount:.2f}" if r.approved_amount is not None else None,
              "assigned_reviewer": r.assigned_reviewer, "sla_due_at": r.sla_due_at.isoformat() if r.sla_due_at else None, "sla_breached": r.sla_breached,
              "received_at": r.received_at.isoformat(), "updated_at": r.updated_at.isoformat(), "etag": r.etag, "patient": mask_name(r.member_name) if r.member_name else None} for r in rows]
    nxt = None
    if more and rows:
        last = rows[-1]
        nxt = base64.urlsafe_b64encode(json.dumps([last.priority, (last.sla_due_at or datetime.max.replace(tzinfo=UTC, year=9999)).isoformat(), str(last.id)]).encode()).decode()
    return {"items": items, "next_cursor": nxt, "limit": limit}


async def _case(s: Any, case_id: UUID) -> ClaimCase:
    c = await s.get(ClaimCase, case_id)
    if c is None:
        raise ProblemError("unknown_claim", "case not found", status=404)
    return c


@router.get("/cases/{case_id}")
async def get_case(case_id: UUID, response: Response, p: Principal = Depends(view)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        c = await _case(s, case_id)
        run = await s.get(VerificationRun, c.latest_run_id) if c.latest_run_id else None
    response.headers["ETag"] = _etag(c)
    return {"id": str(c.id), "insurer_claim_no": c.insurer_claim_no, "hospital_claim_ref": c.hospital_claim_ref, "status": c.status, "claim_type": c.claim_type,
            "admission_type": c.admission_type, "claimed_amount": f"{c.claimed_amount:.2f}", "recommended_amount": f"{c.recommended_amount:.2f}" if c.recommended_amount is not None else None,
            "approved_amount": f"{c.approved_amount:.2f}" if c.approved_amount is not None else None, "priority": c.priority, "assigned_reviewer": c.assigned_reviewer,
            "sla_due_at": c.sla_due_at.isoformat() if c.sla_due_at else None, "sla_breached": c.sla_breached, "degraded": c.degraded, "etag": c.etag,
            "run": {"id": str(run.id), "run_no": run.run_no, "status": run.status, "outcome": run.outcome} if run else None}


@router.get("/cases/{case_id}/workspace")
async def workspace(case_id: UUID, response: Response, p: Principal = Depends(view)) -> dict[str, Any]:
    st = get_settings()
    async with sessionmaker()() as s:
        c = (await s.execute(select(ClaimCase).where(ClaimCase.id == case_id))).scalar_one_or_none()
        if c is None:
            raise ProblemError("unknown_claim", "case not found", status=404)
        sub = (await s.execute(select(ClaimCase.submission).where(ClaimCase.id == case_id))).scalar_one()
        lines = (await s.execute(select(BillLine).where(BillLine.case_id == case_id).order_by(BillLine.line_no))).scalars().all()
        docs = (await s.execute(select(ClaimDocument).where(ClaimDocument.case_id == case_id).order_by(ClaimDocument.created_at))).scalars().all()
        run = await s.get(VerificationRun, c.latest_run_id) if c.latest_run_id else None
        steps = (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run.id))).scalars().all() if run else []
        calc = (await s.execute(select(CalculationResult).where(CalculationResult.case_id == case_id).order_by(CalculationResult.created_at.desc()).limit(1))).scalar_one_or_none()
        rec = (await s.execute(select(Decision).where(Decision.case_id == case_id).order_by(Decision.created_at.desc()))).scalars().all()
        queries = (await s.execute(text("SELECT id, round, status, category, due_by, sent_at FROM core.query WHERE case_id = :c ORDER BY round, created_at"), {"c": case_id})).all()
        overrides = (await s.execute(select(FindingOverride).where(FindingOverride.case_id == case_id))).scalars().all()
        tail = await _audit_tail(s, case_id)
        store = docs_fetch.get_deps().store
        doc_items = []
        for d in docs:
            url = await store.presign(st.minio_bucket, d.object_key, st.presign_ttl_seconds) if d.object_key else None
            doc_items.append({"id": str(d.id), "doc_type": d.doc_type, "filename": d.filename, "pages": d.pages, "fetch_status": d.fetch_status, "scan_result": d.scan_result,
                              "parse_confidence": float(d.parse_confidence) if d.parse_confidence is not None else None, "view_url": url, "superseded": d.superseded_by is not None,
                              "fetch_error": d.fetch_error})
    ded_by_line: dict[str, Any] = {}
    calc_out = calc.output if calc else None
    if calc_out:
        for ln in calc_out["lines"]:
            ded_by_line[ln["line_ref"]] = {"disallowed": ln["disallowed"], "payable": ln["payable"], "rule_ids": sorted({h["rule_id"] for h in ln["rule_trace"]})}
    final = next((d for d in rec if d.kind == "final" and d.status == "finalised"), None)
    latest_rec = next((d for d in rec if d.kind == "recommendation"), None)
    response.headers["ETag"] = _etag(c)
    pat = sub["patient"]
    return {
        "case": {"id": str(c.id), "insurer_claim_no": c.insurer_claim_no, "hospital_claim_ref": c.hospital_claim_ref, "status": c.status, "priority": c.priority,
                 "sla_due_at": c.sla_due_at.isoformat() if c.sla_due_at else None, "etag": c.etag, "assigned_reviewer": c.assigned_reviewer, "claim_type": c.claim_type,
                 "admission_type": c.admission_type, "claimed_amount": f"{c.claimed_amount:.2f}", "degraded": c.degraded},
        "submission": {"patient": {"full_name_masked": mask_name(pat["full_name"]), "dob_year": pat["dob"][:4], "gender": pat["gender"], "member_id_masked": "*" * 4 + pat["member_id"][-4:]},
                       "admission": sub["admission"], "totals": sub["totals"]},
        "bill_lines": [{"line_no": ln.line_no, "line_ref": f"L{ln.line_no:04d}", "description": ln.description, "category": ln.category, "amount": f"{ln.amount:.2f}",
                        "deduction": ded_by_line.get(f"L{ln.line_no:04d}")} for ln in lines],
        "documents": doc_items,
        "run": {"id": str(run.id), "run_no": run.run_no, "status": run.status, "outcome": run.outcome,
                "steps": [{"step": r.step, "status": r.status, "score": float(r.score) if r.score is not None else None, "findings": r.findings, "deterministic": r.deterministic,
                           "agent_note": ((r.agent_output or {}).get("notes")), "degraded": bool((r.agent_output or {}).get("degraded"))}
                          for r in sorted(steps, key=lambda r: STEP_ORDER.index(r.step))]} if run else None,
        "calc": {"engine_version": calc.engine_version, "policy_rules_version": calc.policy_rules_version, "payable_total": calc_out["payable_total"], "flags": calc_out["flags"],
                 "blocked": calc_out["blocked"], "trace": calc_out["trace"], "id": str(calc.id)} if calc and calc_out else None,
        "recommendation": {"outcome": latest_rec.outcome, "approved_amount": f"{latest_rec.approved_amount:.2f}", "reason_codes": latest_rec.reason_codes, "explanation": latest_rec.explanation,
                           "gate_tier": latest_rec.gate_tier, "flags": latest_rec.flags} if latest_rec else None,
        "queries": [{"id": str(q.id), "round": q.round, "status": q.status, "category": q.category, "due_by": q.due_by.isoformat()} for q in queries],
        "decisions": [{"id": str(d.id), "kind": d.kind, "status": d.status, "outcome": d.outcome, "approved_amount": f"{d.approved_amount:.2f}", "tier": d.gate_tier} for d in rec],
        "final_decision": {"outcome": final.outcome, "approved_amount": f"{final.approved_amount:.2f}"} if final else None,
        "overrides": [{"finding_code": o.finding_code, "by": o.overridden_by, "reason": o.reason} for o in overrides],
        "audit_tail": tail,
    }


async def _audit_tail(s: Any, case_id: UUID, n: int = 5) -> list[dict[str, Any]]:
    rows = (await s.execute(text("SELECT seq, event_type, ts, actor_id FROM audit.audit_event WHERE case_id = :c ORDER BY seq DESC LIMIT :n"), {"c": case_id, "n": n})).all()
    return [{"seq": r.seq, "event_type": r.event_type, "ts": r.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "actor_id": r.actor_id} for r in rows]


# ------------------------------------------------------------------ mutations
class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str


@router.post("/cases/{case_id}/assign")
async def assign(case_id: UUID, body: AssignBody, if_match: str | None = Header(default=None), p: Principal = Depends(require_roles("reviewer", "senior_reviewer", "admin"))) -> dict[str, Any]:
    if body.assignee != p.sub and not p.has_any(["senior_reviewer", "admin"]):
        raise ProblemError("forbidden_role", "only a senior reviewer can assign to others", status=403)
    async with transaction() as tx:
        c = await cases.get_for_update(tx.session, case_id)
        cases.check_etag(c, if_match)
        c.assigned_reviewer = body.assignee
        await audit.append(tx.session, c.id, "case.assigned", actor_type="human", actor_id=p.sub, payload={"assignee": body.assignee})
        return cases.case_summary(c)


class PriorityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int


@router.patch("/cases/{case_id}/priority")
async def set_priority(case_id: UUID, body: PriorityBody, if_match: str | None = Header(default=None), p: Principal = Depends(require_roles("senior_reviewer", "admin"))) -> dict[str, Any]:
    if not 1 <= body.priority <= 5:
        raise ProblemError("validation_error", "priority must be 1..5", status=422)
    async with transaction() as tx:
        c = await cases.get_for_update(tx.session, case_id)
        cases.check_etag(c, if_match)
        c.priority = body.priority
        await audit.append(tx.session, c.id, "priority.changed", actor_type="human", actor_id=p.sub, payload={"priority": body.priority})
        return cases.case_summary(c)


class RerunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str] | None = None


@router.post("/cases/{case_id}/verification/rerun", status_code=202)
async def rerun(case_id: UUID, body: RerunBody | None = None, p: Principal = Depends(require_roles("reviewer", "senior_reviewer", "admin"))) -> dict[str, Any]:
    async with sessionmaker()() as s:
        c = await _case(s, case_id)
        if c.status in ("closed", "settled", "approved", "partially_approved", "rejected"):
            raise ProblemError("case_closed", f"case is {c.status}", status=409)
    steps = (body.steps if body and body.steps else None) or ["all"]
    await jobs.enqueue("start_verification", case_id=str(case_id), trigger="manual_rerun", steps=steps)
    return {"accepted": True, "steps": steps}


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/cases/{case_id}/findings/{finding_key}/override")
async def override(case_id: UUID, finding_key: str, body: OverrideBody, p: Principal = Depends(require_roles("reviewer", "senior_reviewer", "admin"))) -> dict[str, Any]:
    return await verification.override_finding(case_id, finding_key, body.reason, p)


@router.get("/cases/{case_id}/runs")
async def runs(case_id: UUID, p: Principal = Depends(view)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        await _case(s, case_id)
        rows = (await s.execute(select(VerificationRun).where(VerificationRun.case_id == case_id).order_by(VerificationRun.run_no))).scalars().all()
    return {"items": [{"id": str(r.id), "run_no": r.run_no, "trigger": r.trigger, "status": r.status, "outcome": r.outcome, "superseded": r.superseded,
                       "config_versions": r.config_versions} for r in rows]}


@router.get("/cases/{case_id}/documents/{doc_id}/download")
async def download(case_id: UUID, doc_id: UUID, p: Principal = Depends(view)) -> RedirectResponse:
    st = get_settings()
    async with transaction() as tx:
        d = await tx.session.get(ClaimDocument, doc_id)
        if d is None or d.case_id != case_id or not d.object_key:
            raise ProblemError("unknown_document", "document not available", status=404)
        url = await docs_fetch.get_deps().store.presign(st.minio_bucket, d.object_key, st.presign_ttl_seconds)
        await audit.append(tx.session, case_id, "doc.viewed", actor_type="human", actor_id=p.sub, payload={"doc_id": str(doc_id)})
    return RedirectResponse(url, status_code=307)


@router.get("/cases/{case_id}/audit")
async def audit_log(case_id: UUID, after_seq: int = 0, limit: int = Query(default=100, ge=1, le=500), p: Principal = Depends(view)) -> dict[str, Any]:
    from claim_contract.insurer_side import audit as caudit

    async with sessionmaker()() as s:
        await _case(s, case_id)
        items = await caudit.read_events(s, case_id, after_seq=after_seq, limit=limit, tables=audit.TABLES)
        head = (await s.execute(text("SELECT last_seq, last_hash FROM audit.case_audit_head WHERE case_id = :c"), {"c": case_id})).one_or_none()
    return {"items": items, "verified": None, "head": {"seq": head.last_seq, "hash": head.last_hash} if head else None}


@router.get("/cases/{case_id}/audit/verify")
async def audit_verify(case_id: UUID, p: Principal = Depends(view)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        await _case(s, case_id)
        res = await audit.verify(s, case_id)
    return {"ok": res.ok, **({"count": res.count} if res.ok else {"seq": res.seq, "reason": res.reason})}  # type: ignore[union-attr]


class RevealBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[str]


@router.post("/cases/{case_id}/pii-reveal")
async def pii_reveal(case_id: UUID, body: RevealBody, p: Principal = Depends(view)) -> dict[str, Any]:
    async with transaction() as tx:
        sub = (await tx.session.execute(select(ClaimCase.submission).where(ClaimCase.id == case_id))).scalar_one_or_none()
        if sub is None:
            raise ProblemError("unknown_claim", "case not found", status=404)
        allowed = {"full_name": sub["patient"]["full_name"], "dob": sub["patient"]["dob"]}
        values = {k: allowed[k] for k in body.fields if k in allowed}
        await audit.append(tx.session, case_id, "pii.revealed", actor_type="human", actor_id=p.sub, payload={"fields": sorted(values)})
    return {"values": values, "ttl_seconds": 60}


@router.post("/cases/{case_id}/presence", status_code=204)
async def presence(case_id: UUID, request: Request, p: Principal = Depends(view)) -> Response:
    r = getattr(request.app.state, "redis", None)
    if r is not None:
        try:
            await r.set(f"presence:{case_id}:{p.sub}", "1", ex=30)
        except Exception:
            pass
    return Response(status_code=204)


_ = (events, Decimal)
