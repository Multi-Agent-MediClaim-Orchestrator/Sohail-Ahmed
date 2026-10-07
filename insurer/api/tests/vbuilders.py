"""Builders for pure verification-engine tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.config.defaults import DOC_REQUIREMENTS, THRESHOLDS, policy_rules
from app.config.schemas import DocRequirements, PolicyRulesPayload, Thresholds
from app.verification.context import DocInfo, HospitalInfo, MemberInfo, PatientInfo, PolicyInfo, VCtx

REQUIRED = ["discharge_summary", "final_bill", "itemised_bill", "claim_form", "id_proof", "policy_card"]


def doc(i: int, t: str, **kw: Any) -> DocInfo:
    base = dict(id=f"d{i}", doc_type=t, pages=2, sha256=f"{i:064x}", fetch_status="fetched", parse_confidence=0.95)
    base.update(kw)
    return DocInfo(**base)  # type: ignore[arg-type]


def make_ctx(**ov: Any) -> VCtx:
    docs = ov.pop("documents", None)
    if docs is None:
        docs = [doc(i, t, vision={"stamp_detected": True, "signature_detected": True, "tamper_score": 0.02}) for i, t in enumerate(REQUIRED)]
    ctx = VCtx(
        case_id="c1", claim_type="cashless", admission_type="planned", admitted_on=date(2026, 9, 1), discharged_on=date(2026, 9, 4),
        claimed_amount=Decimal("65500.00"), claimed_gross=Decimal("65500.00"), diagnosis_codes=["K35.8"], procedure_codes=[], procedure_group=None,
        hospital=HospitalInfo("HOSP-0001", "network", date(2027, 12, 31)),
        patient=PatientInfo("Asha Verma", date(1984, 3, 12), "F", "MEM-77120345", "POL-NIV-2025-004411", "ab" * 32),
        policy=PolicyInfo("active", date(2026, 1, 1), date(2026, 12, 31), date(2026, 12, 31), 30, Decimal("500000"), Decimal("0"), "HEALTH-PLUS-GOLD"),
        member=MemberInfo("Asha Verma", date(1984, 3, 12), "F", "ab" * 32, date(2025, 1, 1), ()),
        utilised=Decimal("0"), documents=docs, bill_categories={"room", "surgery"},
        docs_cfg=DocRequirements.model_validate({"schema_id": "doc_requirements@1", **DOC_REQUIREMENTS}),
        thresholds=Thresholds.model_validate({"schema_id": "thresholds@1", **THRESHOLDS}),
        rules=PolicyRulesPayload.model_validate({"schema_id": "policy_rules@1", **policy_rules("HEALTH-PLUS-GOLD")}),
    )
    return replace(ctx, **ov)


def with_member(ctx: VCtx, **kw: Any) -> VCtx:
    return replace(ctx, member=replace(ctx.member, **kw))


def with_patient(ctx: VCtx, **kw: Any) -> VCtx:
    return replace(ctx, patient=replace(ctx.patient, **kw))


def with_policy(ctx: VCtx, **kw: Any) -> VCtx:
    return replace(ctx, policy=replace(ctx.policy, **kw))
