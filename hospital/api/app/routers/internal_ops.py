"""Endpoints the n8n flows depend on besides the document callbacks (doc 08 §4): reminders, notifications,
ops events and "what is stuck" lists. Service accounts only."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.auth.deps import require_service
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.errors import ApiError
from app.core.uow import UoW

router = APIRouter(prefix="/v1/internal", tags=["internal-ops"])
N8n = Depends(require_service("svc-n8n", "svc-internal"))
Svc = Depends(require_service("svc-n8n", "svc-crew", "svc-internal"))
log = logging.getLogger("app.ops")
MAX_ATTEMPTS = 3


class NotifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(pattern=r"^[a-z_]+\.[a-z_.]+$", max_length=60)
    data: dict[str, Any] = Field(default_factory=dict)


class OpsEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    workflow: str = Field(max_length=100)
    execution_id: str | None = Field(default=None, max_length=100)
    node: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=2000)
    severity: str = Field(default="medium", pattern="^(low|medium|high)$")
    correlation_id: str | None = Field(default=None, max_length=100)


class FiredIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=500)


def _cid(v: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise ApiError("not_found", "unknown id") from None


@router.post("/cases/{case_id}/notify", operation_id="internalNotify", status_code=202)
async def notify(
    case_id: str, body: NotifyIn, request: Request, _: Principal = Svc, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    cid = _cid(case_id)
    if not (
        await uow.session.execute(text("SELECT 1 FROM claim_case WHERE id=:i"), {"i": cid})
    ).first():
        raise ApiError("not_found", "unknown case")
    await request.app.state.hub.publish(body.event, str(cid), body.data)
    return {"accepted": True}


@router.post("/ops/events", operation_id="internalOpsEvent", status_code=202)
async def ops_event(body: OpsEvent, request: Request, _: Principal = Svc) -> dict[str, Any]:
    log.error("ops_event", extra={"ops": body.model_dump()})
    await request.app.state.hub.publish(
        "system.ops_event", None, {"workflow": body.workflow, "severity": body.severity}
    )
    return {"accepted": True}


@router.get("/reminders/due", operation_id="internalRemindersDue")
async def reminders_due(
    limit: int = Query(50, ge=1, le=200), _: Principal = N8n, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT id, case_id, kind, channel, payload, attempts FROM reminder WHERE status='scheduled' AND fire_at <= now() "
                "ORDER BY fire_at LIMIT :n"
            ),
            {"n": limit},
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "case_id": str(r.case_id),
                "kind": r.kind,
                "channel": r.channel,
                "vars": r.payload or {},
            }
            for r in rows
        ]
    }


@router.post("/reminders/{rid}/fired", operation_id="internalReminderFired")
async def reminder_fired(
    rid: str, body: FiredIn, request: Request, _: Principal = N8n, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    row = (
        await uow.session.execute(
            text(
                "UPDATE reminder SET status='fired', fired_at=now() WHERE id=:i AND status='scheduled' RETURNING case_id, kind"
            ),
            {"i": _cid(rid)},
        )
    ).first()
    await uow.commit()
    if row:  # in-app reminders surface through the event stream
        await request.app.state.hub.publish("reminder.due", str(row.case_id), {"kind": row.kind})
    return {"status": "fired" if row else "ignored"}


@router.post("/reminders/{rid}/failed", operation_id="internalReminderFailed")
async def reminder_failed(
    rid: str, body: FiredIn, _: Principal = N8n, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    row = (
        await uow.session.execute(
            text(
                "UPDATE reminder SET attempts=attempts+1, last_error=:e, status=CASE WHEN attempts+1 >= :m THEN 'failed' ELSE status END "
                "WHERE id=:i AND status='scheduled' RETURNING status"
            ),
            {"i": _cid(rid), "e": body.error, "m": MAX_ATTEMPTS},
        )
    ).first()
    await uow.commit()
    return {"status": row.status if row else "ignored"}


@router.get("/queries/{query_id}", operation_id="internalGetQuery")
async def get_query(
    query_id: str, _: Principal = Svc, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    r = (
        await uow.session.execute(
            text(
                "SELECT q.id, q.case_id, q.round, q.category::text AS category, q.text, q.due_by, q.requested_doc_types::text[] AS docs, "
                "q.triage, q.escalation_risk FROM insurer_query q WHERE q.id=:i"
            ),
            {"i": _cid(query_id)},
        )
    ).first()
    if r is None:
        raise ApiError("not_found", "unknown query")
    return {
        "id": str(r.id),
        "case_id": str(r.case_id),
        "round": r.round,
        "category": r.category,
        "text": r.text,
        "due_by": r.due_by.isoformat() if r.due_by else None,
        "requested_doc_types": r.docs,
        "triage": r.triage,
        "escalation_risk": r.escalation_risk,
    }


@router.get("/cases/stale-building", operation_id="internalStaleBuilding")
async def stale_building(
    older_than_s: int = Query(900, ge=0), _: Principal = N8n, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT id FROM claim_case WHERE status='building_claim' AND updated_at < now() - make_interval(secs => :s)"
            ),
            {"s": older_than_s},
        )
    ).all()
    return {"items": [{"case_id": str(r.id)} for r in rows]}


@router.get("/outbox/stalled", operation_id="internalOutboxStalled")
async def outbox_stalled(_: Principal = N8n, uow: UoW = Depends(get_uow)) -> dict[str, Any]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT case_id, kind, status::text AS status FROM outbox WHERE status IN ('dead') OR (status='sending' AND next_attempt_at < now() - interval '10 minutes')"
            )
        )
    ).all()
    return {
        "items": [{"case_id": str(r.case_id), "kind": r.kind, "status": r.status} for r in rows]
    }


class IdemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    key: str = Field(min_length=1, max_length=200)
    ttl_s: int = Field(default=3600, ge=1, le=86400)


@router.post("/idempotency", operation_id="internalIdempotency")
async def idempotency(body: IdemIn, request: Request, _: Principal = N8n) -> dict[str, bool]:
    """SET NX with a TTL; n8n flows use it to drop duplicate webhook deliveries."""
    ok = await request.app.state.redis.set(
        f"idem:hosp:n8n:{body.workflow}:{body.key}", "1", nx=True, ex=body.ttl_s
    )
    return {"duplicate": not ok}


@router.get("/documents/{doc_id}", operation_id="internalGetDocument")
async def get_document(
    doc_id: str, request: Request, _: Principal = Svc, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    """What a pipeline worker needs: scan result, a short-lived URL and the case. No PII."""
    r = (
        await uow.session.execute(
            text(
                "SELECT id, case_id, scan_status, parse_status, lifecycle, storage_key, original_filename, mime_type, "
                "doc_type::text AS dtype FROM document WHERE id=:i"
            ),
            {"i": _cid(doc_id)},
        )
    ).first()
    if r is None or r.lifecycle != "active":
        raise ApiError("not_found", "unknown document")
    url = None
    if r.scan_status == "clean" and r.storage_key:
        st = request.app.state
        url = await st.store.presign_get(r.storage_key, 900)
    return {
        "id": str(r.id),
        "case_id": str(r.case_id),
        "scan_status": r.scan_status,
        "parse_status": r.parse_status,
        "doc_type": r.dtype,
        "mime_type": r.mime_type,
        "presigned_url": url,
    }


@router.get("/cases/{case_id}/ack-status", operation_id="internalAckStatus")
async def ack_status(
    case_id: str, _: Principal = N8n, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    r = (
        await uow.session.execute(
            text("SELECT acknowledged_at, status::text AS status FROM claim_case WHERE id=:i"),
            {"i": _cid(case_id)},
        )
    ).first()
    if r is None:
        raise ApiError("not_found", "unknown case")
    return {"acknowledged": r.acknowledged_at is not None, "status": r.status}


@router.get("/queries/{query_id}/context", operation_id="internalQueryContext")
async def query_context(
    query_id: str, _: Principal = Svc, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    from app.services import queries as qsvc

    return await qsvc.query_context(uow, query_id)


@router.get("/cases/{case_id}/build-context", operation_id="internalBuildContext")
async def build_context(
    case_id: str, _: Principal = Svc, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    """Facts for the claim builder: authoritative case data, typed document values, the previous draft."""
    s = uow.session
    cid = _cid(case_id)
    c = (
        await s.execute(
            text(
                "SELECT c.*, c.admission_type::text AS adm_t, p.full_name, p.dob, p.gender, pol.member_id, pol.policy_number "
                "FROM claim_case c JOIN patient p ON p.id=c.patient_id JOIN insurance_policy_ref pol ON pol.id=c.policy_ref_id "
                "WHERE c.id=:i"
            ),
            {"i": cid},
        )
    ).first()
    if c is None:
        raise ApiError("not_found", "unknown case")
    docs = (
        await s.execute(
            text(
                "SELECT d.id, d.doc_type::text AS dtype, d.pages, "
                "(SELECT jsonb_object_agg(k, v) FROM (SELECT (jsonb_each(p.typed_json)).* FROM document_parse p "
                "WHERE p.document_id = d.id ORDER BY p.pass_no) x(k, v) WHERE jsonb_typeof(v) <> 'null') AS typed "
                "FROM document d WHERE d.case_id=:c AND d.lifecycle='active' AND d.scan_status='clean' "
                "AND d.doc_type IS NOT NULL ORDER BY d.created_at"
            ),
            {"c": cid},
        )
    ).all()
    prev = (
        await s.execute(
            text("SELECT payload FROM claim_draft WHERE case_id=:c ORDER BY version DESC LIMIT 1"),
            {"c": cid},
        )
    ).first()
    return {
        "claim_type": str(c.claim_type.value if hasattr(c.claim_type, "value") else c.claim_type),
        "case": {
            "patient": {
                "full_name": c.full_name,
                "dob": c.dob.isoformat(),
                "gender": c.gender,
                "member_id": c.member_id,
                "policy_number": c.policy_number,
            },
            "admission": {
                "admission_type": c.adm_t,
                "admitted_on": c.admitted_on.isoformat() if c.admitted_on else None,
                "discharged_on": c.discharged_on.isoformat() if c.discharged_on else None,
                "diagnosis_codes": list(c.diagnosis_codes or []),
                "procedure_codes": list(c.procedure_codes or []),
                "treating_doctor": c.treating_doctor or "",
                "hospital_id": None,
                "preauth_ref": c.preauth_ref,
            },
        },
        "documents": [
            {"id": str(d.id), "doc_type": d.dtype, "pages": d.pages or 1, "typed": d.typed or {}}
            for d in docs
        ],
        "previous_draft": prev.payload if prev else None,
    }


@router.get("/hospitals", operation_id="internalHospitals")
async def hospitals(_: Principal = Svc, uow: UoW = Depends(get_uow)) -> dict[str, Any]:
    """The hospital registry the vision service matches stamp text against."""
    rows = (
        await uow.session.execute(text("SELECT code, name, rohini_id FROM hospital ORDER BY code"))
    ).all()
    return {
        "items": [
            {"code": r.code, "name": r.name, "rohini_id": r.rohini_id, "aliases": []} for r in rows
        ]
    }
