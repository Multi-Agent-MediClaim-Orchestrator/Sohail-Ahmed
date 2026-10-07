"""Builds the pure ``VCtx`` (and the PII-minimised crew context) from the database."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.service import ConfigNotFound, ConfigService
from ..models.core import (
    BillLine,
    ClaimCase,
    ClaimDocument,
    InsuranceProduct,
    NetworkHospital,
    Policy,
    PolicyMember,
)
from ..verification.context import (
    DocInfo,
    HospitalInfo,
    MemberInfo,
    Overlap,
    PatientInfo,
    PolicyInfo,
    VCtx,
)
from . import utilisation


async def build_vctx(session: AsyncSession, cfg: ConfigService, case: ClaimCase, *, at: Any = None) -> VCtx:
    sub = (await session.execute(select(ClaimCase.submission).where(ClaimCase.id == case.id))).scalar_one()
    hosp = await session.get(NetworkHospital, case.hospital_id)
    assert hosp is not None
    policy = await session.get(Policy, case.policy_id) if case.policy_id else None
    member = await session.get(PolicyMember, case.member_id) if case.member_id else None
    product_code = None
    if policy is not None:
        product_code = (await session.get(InsuranceProduct, policy.product_id)).code  # type: ignore[union-attr]
    docs = (await session.execute(select(ClaimDocument).where(ClaimDocument.case_id == case.id).order_by(ClaimDocument.created_at))).scalars().all()
    lines = (await session.execute(select(BillLine).where(BillLine.case_id == case.id))).scalars().all()
    adm = sub["admission"]
    from datetime import date

    admitted, discharged = date.fromisoformat(adm["admitted_on"]), date.fromisoformat(adm["discharged_on"])
    util = Decimal("0")
    if policy is not None and admitted >= policy.start_date:
        util = await utilisation.utilised(session, policy.id, utilisation.policy_year(policy.start_date, admitted))
    # overlapping claims: same member + hospital, overlapping stay, not rejected/closed (duplicate-claim detection)
    overlapping: list[Overlap] = []
    if case.member_id:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.insurer_claim_no, c.status::text AS status, c.submission #>> '{admission,admitted_on}' AS a, "
                    "c.submission #>> '{admission,discharged_on}' AS d FROM core.claim_case c WHERE c.member_id = :m AND c.hospital_id = :h "
                    "AND c.id <> :id AND c.status::text NOT IN ('rejected','closed')"
                ),
                {"m": case.member_id, "h": case.hospital_id, "id": case.id},
            )
        ).all()
        for r in rows:
            if date.fromisoformat(r.a) <= discharged and admitted <= date.fromisoformat(r.d):
                overlapping.append(Overlap(str(r.id), r.insurer_claim_no, r.status))
    shared: list[str] = []
    if docs and case.member_id:  # without a resolved member we cannot say the file belongs to *another* member
        hashes = (
            await session.execute(
                text(
                    "SELECT DISTINCT d2.sha256 FROM core.claim_document d2 JOIN core.claim_case oc ON oc.id = d2.case_id "
                    "WHERE d2.sha256 = ANY(:hs) AND oc.id <> :id AND oc.member_id IS NOT NULL AND oc.member_id <> :m"
                ),
                {"hs": [d.sha256 for d in docs], "id": case.id, "m": case.member_id},
            )
        ).scalars().all()
        shared = [str(d.id) for d in docs if d.sha256 in set(hashes)]
    docs_cfg = (await cfg.resolve(session, "doc_requirements", "default", at)).payload
    thresholds = (await cfg.resolve(session, "thresholds", "default", at))
    rules = None
    versions: dict[str, Any] = {"doc_requirements": (await cfg.resolve(session, "doc_requirements", "default", at)).version, "thresholds": thresholds.version}
    if product_code:
        try:
            pr = await cfg.resolve(session, "policy_rules", product_code, at)
            rules, versions["policy_rules"] = pr.payload, pr.version
        except ConfigNotFound:
            pass
    try:
        versions["query_policy"] = (await cfg.resolve(session, "query_policy", "default", at)).version
    except ConfigNotFound:
        pass
    pat = sub["patient"]
    return VCtx(
        case_id=str(case.id), claim_type=case.claim_type, admission_type=case.admission_type, admitted_on=admitted, discharged_on=discharged,
        claimed_amount=case.claimed_amount, claimed_gross=Decimal(sub["totals"]["gross"]["amount"]), diagnosis_codes=list(adm["diagnosis_codes"]),
        procedure_codes=list(adm.get("procedure_codes") or []), procedure_group=case.procedure_group,
        hospital=HospitalInfo(hosp.hospital_code, hosp.network_status, hosp.empanelment_valid_till, hosp.watchlist),
        patient=PatientInfo(pat["full_name"], date.fromisoformat(pat["dob"]), pat["gender"], pat["member_id"], pat["policy_number"], pat.get("id_proof_hash")),
        policy=PolicyInfo(policy.status, policy.start_date, policy.end_date, policy.premium_paid_until, policy.grace_days, policy.sum_insured,
                          policy.cumulative_bonus, product_code or "") if policy else None,
        member=MemberInfo(member.full_name, member.dob, member.gender, member.id_proof_hash, member.cover_start, tuple(member.pre_existing or ())) if member else None,
        utilised=util,
        documents=[DocInfo(str(d.id), d.doc_type, d.pages, d.sha256, d.fetch_status, float(d.parse_confidence) if d.parse_confidence is not None else None,
                           d.superseded_by is not None, d.vision, d.extract) for d in docs],
        bill_categories={ln.category for ln in lines}, docs_cfg=docs_cfg, thresholds=thresholds.payload, rules=rules,
        overlapping=overlapping, shared_hash_docs=shared, config_versions=versions,
    )


def minimised_context(ctx: VCtx, step: str) -> dict[str, Any]:
    """PII-minimised bundle for crews (03-03 §4.2 /context): no id_proof_hash unless identity needs it; no raw ids."""
    base: dict[str, Any] = {"case_id": ctx.case_id, "claim_type": ctx.claim_type, "admission_type": ctx.admission_type,
                            "admitted_on": ctx.admitted_on.isoformat(), "diagnosis_codes": ctx.diagnosis_codes, "config_versions": ctx.config_versions}
    if step == "identity":
        base["patient"] = {"full_name": ctx.patient.full_name, "dob": ctx.patient.dob.isoformat(), "gender": ctx.patient.gender}
        if ctx.member:
            base["member"] = {"full_name": ctx.member.full_name, "dob": ctx.member.dob.isoformat(), "gender": ctx.member.gender}
    elif step == "coverage":
        base["policy"] = {"product_code": ctx.policy.product_code if ctx.policy else None}
        base["procedure_codes"] = ctx.procedure_codes
    elif step == "authenticity":
        base["documents"] = [{"doc_id": d.id, "doc_type": d.doc_type, "pages": d.pages, "vision": d.vision, "extract": d.extract} for d in ctx.fetched_docs]
    return base


_ = UUID
