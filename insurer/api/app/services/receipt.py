"""Claim receipt (03-02 §6.1): validate -> one transaction (case, lines, docs, audit, outbox) -> enqueue jobs -> ack."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from claim_contract.audit import canonical_json
from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import ProblemError
from claim_contract.models import Acknowledgement, ClaimSubmission
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..config.service import ConfigNotFound, ConfigService
from ..db import Tx, sessionmaker, transaction
from ..ids import uuid7
from ..models.core import BillLine, ClaimCase, ClaimDocument, NetworkHospital, Policy, PolicyMember
from ..security.urlcrypt import encrypt_url
from ..settings import Settings, get_settings
from ..validators import (
    assign_line_ids,
    check_door_rules,
    check_hospital,
    parse_submission,
    priority_for,
)
from ..verification.groups import derive_procedure_group
from . import audit, events, jobs, outbox


def submission_hash(sub: ClaimSubmission) -> str:
    return hashlib.sha256(canonical_json(sub.model_dump(mode="json", exclude_none=True)).encode()).hexdigest()


async def next_claim_no(session: AsyncSession) -> str:
    n = (await session.execute(text("SELECT nextval('ops.claim_no_seq')"))).scalar_one()
    return f"IC-{clock.now().year}-{n:06d}"


def ack_from(case: ClaimCase, ndocs: int = 0, failed: int = 0) -> Acknowledgement:
    return Acknowledgement(
        claim_ref=case.hospital_claim_ref, insurer_claim_no=case.insurer_claim_no, status=InsurerCaseStatus(case.status),
        received_at=case.received_at, sequence=1, document_ingest={"queued": ndocs, "failed": failed}, contract_version=case.contract_version,
    )


async def sla_for(cfg: ConfigService, session: AsyncSession, sub: ClaimSubmission) -> tuple[Any, timedelta]:
    th = await cfg.resolve(session, "thresholds", "default")
    key = ("cashless_emergency" if sub.admission.admission_type.value == "emergency" else "cashless_planned") if sub.claim_type.value == "cashless" else "reimbursement"
    return th, timedelta(hours=th.payload.sla_hours.get(key, 24))


async def receive_claim(raw_body: bytes, key_id: str, cfg: ConfigService, settings: Settings | None = None, sm: Any = None) -> tuple[Acknowledgement, bool]:
    """Returns (ack, replayed). Raises ``ProblemError`` for every documented failure."""
    s = settings or get_settings()
    sub = parse_submission(raw_body)
    async with (sm or sessionmaker())() as session:
        hosp = (await session.execute(select(NetworkHospital).where(NetworkHospital.hmac_key_id == key_id))).scalar_one_or_none()
    try:
        hosp = check_hospital(sub, hosp, key_id)
    except ProblemError as exc:
        if exc.code in ("hospital_blacklisted", "hospital_mismatch") and hosp is not None:
            # audit the refusal on a deterministic per-claim chain (the case does not exist)
            cid = uuid.uuid5(uuid.NAMESPACE_URL, f"refused:{hosp.hospital_code}:{sub.claim_ref}")
            async with transaction(sm) as t2:
                await audit.append(t2.session, cid, "claim.refused", payload={"reason": exc.code, "hospital": hosp.hospital_code, "claim_ref": sub.claim_ref})
        raise
    report = check_door_rules(sub, s.allowed_doc_hosts, clock.now())
    assign_line_ids(sub)
    h = submission_hash(sub)
    async with transaction(sm) as tx:
        session = tx.session
        existing = (
            await session.execute(
                select(ClaimCase).where(ClaimCase.hospital_id == hosp.id, ClaimCase.hospital_claim_ref == sub.claim_ref).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.submission_hash == h:
                return ack_from(existing, len(sub.documents)), True
            raise ProblemError("idempotency_conflict", "claim_ref already submitted with different content; use the supplement endpoint")
        policy = (await session.execute(select(Policy).where(Policy.policy_number == sub.patient.policy_number))).scalar_one_or_none()
        member = (await session.execute(select(PolicyMember).where(PolicyMember.member_id == sub.patient.member_id))).scalar_one_or_none()
        th, sla = await sla_for(cfg, session, sub)
        now = clock.now()
        snapshot = await _config_snapshot(cfg, session, policy, tx)
        case = ClaimCase(
            id=uuid7(), insurer_claim_no=await next_claim_no(session), hospital_claim_ref=sub.claim_ref, hospital_id=hosp.id,
            claim_type=sub.claim_type.value, admission_type=sub.admission.admission_type.value, status="received",
            policy_id=policy.id if policy else None, member_id=member.id if member else None, claimed_amount=sub.totals.claimed.amount,
            contract_version=sub.contract_version, submission=json.loads(sub.model_dump_json()), submission_hash=h, received_at=now,
            sla_due_at=now + sla, priority=priority_for(sub, th.payload.t_auto_inr), last_callback_seq=1, journey_id=sub.journey_id,
            procedure_group=derive_procedure_group(sub.admission.diagnosis_codes, sub.admission.procedure_codes), config_snapshot=snapshot,
        )
        session.add(case)
        try:
            await session.flush()
        except IntegrityError as exc:  # concurrent identical submission lost the race
            raise ProblemError("idempotency_in_progress", "claim is being received", headers={"Retry-After": "2"}) from exc
        session.add_all(_documents(case, sub))
        await session.flush()
        session.add_all(_bill_lines(case, sub))
        await session.flush()
        await audit.append(
            session, case.id, "claim.received", payload={"hospital": hosp.hospital_code, "claimed": str(case.claimed_amount), "docs": len(sub.documents),
                                                         "claim_ref": sub.claim_ref, "warnings": [w.rule for w in report.warnings]},
            config_versions=snapshot, journey_id=case.journey_id,
        )
        await outbox.enqueue_status(session, case, note=None)
        cid, no = case.id, case.insurer_claim_no
        tx.on_commit(lambda: jobs.enqueue("fetch_documents", case_id=str(cid)))
        tx.on_commit(lambda: jobs.enqueue("start_verification", case_id=str(cid)))
        tx.on_commit(lambda: events.publish("case.received", cid, {"claim_ref": sub.claim_ref, "claim_type": sub.claim_type.value}, ["reviewer", "senior_reviewer", "admin"], no))
        return ack_from(case, len(sub.documents)), False


async def _config_snapshot(cfg: ConfigService, session: AsyncSession, policy: Policy | None, tx: Tx) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for dom in ("thresholds", "query_policy", "doc_requirements"):
        try:
            snap[dom] = (await cfg.resolve(session, dom, "default")).version
        except ConfigNotFound:
            snap[dom] = None
    try:
        if policy is not None:
            code = (await session.execute(text("SELECT code FROM core.insurance_product WHERE id = :i"), {"i": policy.product_id})).scalar_one()
            snap["policy_rules"] = (await cfg.resolve(session, "policy_rules", code)).version
    except ConfigNotFound:
        snap["policy_rules"] = None
    return snap


def _bill_lines(case: ClaimCase, sub: ClaimSubmission) -> list[BillLine]:
    out = []
    for i, bl in enumerate(sub.bill_lines, start=1):
        out.append(BillLine(
            id=uuid7(), case_id=case.id, line_no=i, code=bl.code, description=bl.description, category=bl.category.value, qty=bl.qty,
            unit_price=bl.unit_price.amount, amount=bl.amount.amount, service_date=bl.service_date, source_doc_id=bl.source_doc_id, source_page=bl.source_page,
        ))
    return out


def _documents(case: ClaimCase, sub: ClaimSubmission, *, via_query: Any = None) -> list[ClaimDocument]:
    return [
        ClaimDocument(
            id=d.doc_id, case_id=case.id, doc_type=d.doc_type.value, filename=d.filename, sha256=d.sha256, size_bytes=d.size_bytes, pages=d.pages,
            parse_confidence=Decimal(str(d.parse_confidence)).quantize(Decimal("0.001")) if d.parse_confidence is not None else None,
            source_url_enc=encrypt_url(str(d.download_url)), fetch_status="pending", added_via_query=via_query,
        )
        for d in sub.documents
    ]
