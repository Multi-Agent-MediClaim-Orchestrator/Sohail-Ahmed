"""Hospital-facing claim operations besides receipt: status, supplement, withdraw (03-02 §4.2-4.8)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from claim_contract.enums import INSURER_TO_HOSPITAL, InsurerCaseStatus
from claim_contract.errors import ProblemError
from claim_contract.models import DocSupplement, StatusUpdate, WithdrawRequest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..db import transaction
from ..models.core import ClaimCase, ClaimDocument, NetworkHospital, Query
from ..security.ssrf import UrlNotAllowed, assert_allowed_host, parse_allow_list
from ..security.urlcrypt import encrypt_url
from ..settings import Settings, get_settings
from . import audit, cases, decision_builder, events, jobs

SUPPLEMENT_STATES = {"received", "verifying", "needs_info", "ready_for_decision"}
WITHDRAW_STATES = SUPPLEMENT_STATES


async def hospital_for_key(session: AsyncSession, key_id: str) -> NetworkHospital:
    h = (await session.execute(select(NetworkHospital).where(NetworkHospital.hmac_key_id == key_id))).scalar_one_or_none()
    if h is None:
        raise ProblemError("invalid_signature", "signature verification failed")
    return h


async def own_case(session: AsyncSession, hospital: NetworkHospital, claim_ref: str, *, for_update: bool = False) -> ClaimCase:
    q = select(ClaimCase).where(ClaimCase.hospital_id == hospital.id, ClaimCase.hospital_claim_ref == claim_ref)
    case = (await session.execute(q.with_for_update() if for_update else q)).scalar_one_or_none()
    if case is None:  # 404, never 403: do not leak the existence of other hospitals' claims
        raise ProblemError("unknown_claim", "claim not found", status=404)
    return case


def _query_obj(q: Query) -> dict[str, Any]:
    return {"query_id": str(q.id), "round": q.round, "category": q.category, "text": q.text, "requested_doc_types": list(q.requested_doc_types or []),
            "due_by": q.due_by.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": q.status, "raised_by": q.raised_by or "agent:query-drafter+human"}


async def status_for(session: AsyncSession, hospital: NetworkHospital, claim_ref: str, include_queries: bool = False) -> dict[str, Any]:
    case = await own_case(session, hospital, claim_ref)
    st = InsurerCaseStatus(case.status)
    open_ids = [r for r in (await session.execute(select(Query.id).where(Query.case_id == case.id, Query.status == "open", Query.sent_at.is_not(None)))).scalars()]
    upd = StatusUpdate(
        claim_ref=case.hospital_claim_ref, insurer_claim_no=case.insurer_claim_no, status=st, hospital_visible_status=INSURER_TO_HOSPITAL[st],
        sequence=case.last_callback_seq, occurred_at=case.updated_at or clock.now(), open_query_ids=open_ids,
        decision=await decision_builder.contract_decision(session, case),
    )
    body = upd.model_dump(mode="json")
    # internal statuses ready_for_decision / awaiting_approval are never leaked: only the mapped value crosses the boundary
    body["status"] = INSURER_TO_HOSPITAL[st].value
    body["status_since"] = body["occurred_at"]
    if include_queries:
        qs = (await session.execute(select(Query).where(Query.case_id == case.id, Query.sent_at.is_not(None)).order_by(Query.round, Query.created_at))).scalars().all()
        body["queries"] = [_query_obj(q) for q in qs]
    return body


async def supplement(sup: DocSupplement, hospital: NetworkHospital, claim_ref: str, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    allow = parse_allow_list(s.allowed_doc_hosts)
    for i, d in enumerate(sup.documents):
        try:
            assert_allowed_host(str(d.download_url), allow)
        except UrlNotAllowed as exc:
            raise ProblemError("validation_error", f"download_url not allowed: {exc}", errors=[{"field": f"documents[{i}].download_url", "message": str(exc)}]) from exc
    async with transaction() as tx:
        case = await own_case(tx.session, hospital, claim_ref, for_update=True)
        if case.status not in SUPPLEMENT_STATES:
            raise ProblemError("invalid_transition", f"claim is {case.status}; documents can no longer be supplemented")
        open_types: set[str] = set()
        if sup.reason.value == "query_response":
            for q in (await tx.session.execute(select(Query).where(Query.case_id == case.id, Query.status == "open"))).scalars():
                open_types |= set(q.requested_doc_types or [])
        existing = (await tx.session.execute(select(ClaimDocument).where(ClaimDocument.case_id == case.id, ClaimDocument.superseded_by.is_(None)))).scalars().all()
        superseded: list[str] = []
        for d in sup.documents:
            dup = await tx.session.get(ClaimDocument, d.doc_id)
            if dup is not None:  # idempotent re-send of the same doc id
                continue
            doc = ClaimDocument(id=d.doc_id, case_id=case.id, doc_type=d.doc_type.value, filename=d.filename, sha256=d.sha256, size_bytes=d.size_bytes,
                                pages=d.pages, source_url_enc=encrypt_url(str(d.download_url)), fetch_status="pending", added_via_query=sup.query_id)
            tx.session.add(doc)
            await tx.session.flush()
            if sup.reason.value == "query_response" and d.doc_type.value in open_types:
                for old in existing:
                    if old.doc_type == d.doc_type.value and old.superseded_by is None:
                        old.superseded_by = d.doc_id
                        superseded.append(str(old.id))
        case.last_inbound_seq += 1
        seq = case.last_inbound_seq
        await audit.append(tx.session, case.id, "docs.supplemented", actor_type="external", actor_id=f"hospital:{hospital.hospital_code}",
                           payload={"accepted": len(sup.documents), "reason": sup.reason.value, "superseded": superseded}, journey_id=case.journey_id)
        cid = case.id
        tx.on_commit(lambda: jobs.enqueue("fetch_documents", case_id=str(cid)))
    return {"accepted": len(sup.documents), "superseded": superseded, "sequence": seq}


async def withdraw(req: WithdrawRequest, hospital: NetworkHospital, claim_ref: str) -> dict[str, Any]:
    async with transaction() as tx:
        case = await own_case(tx.session, hospital, claim_ref, for_update=True)
        if case.status not in WITHDRAW_STATES:
            raise ProblemError("invalid_transition", f"claim is {case.status}; it can no longer be withdrawn")
        await tx.session.execute(
            text("UPDATE core.query SET status = 'closed', closed_reason = 'claim_withdrawn' WHERE case_id = :c AND status IN ('open','draft_ready')"), {"c": case.id}
        )
        await tx.session.execute(text("DELETE FROM ops.outbox WHERE case_id = :c AND status = 'pending'"), {"c": case.id})
        case.closure_reason = "withdrawn_by_hospital"
        await audit.append(tx.session, case.id, "claim.withdrawn", actor_type="external", actor_id=f"hospital:{hospital.hospital_code}",
                           payload={"reason": req.reason.value, "note_len": len(req.note or "")}, journey_id=case.journey_id)
        await cases.transition(tx, case, InsurerCaseStatus.closed, actor_type="external", actor_id=f"hospital:{hospital.hospital_code}", reason="withdrawn_by_hospital")
        no, cid = case.insurer_claim_no, case.id
        tx.on_commit(lambda: events.publish("case.status_changed", cid, {"to": "closed", "reason": "withdrawn_by_hospital"}, insurer_claim_no=no))
    return {"claim_ref": claim_ref, "status": "closed"}


async def list_queries(session: AsyncSession, hospital: NetworkHospital, claim_ref: str, limit: int, cursor: str | None) -> dict[str, Any]:
    case = await own_case(session, hospital, claim_ref)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    rows = (await session.execute(select(Query).where(Query.case_id == case.id, Query.sent_at.is_not(None)).order_by(Query.round, Query.created_at).offset(offset).limit(limit + 1))).scalars().all()
    more = len(rows) > limit
    return {"items": [_query_obj(q) for q in rows[:limit]], "next_cursor": str(offset + limit) if more else None, "limit": limit}


_ = UUID
