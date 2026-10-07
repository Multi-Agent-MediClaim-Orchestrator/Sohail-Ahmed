"""Build the contract ``Decision`` (and its config stamps) from stored rows (03-04 §5 task 7)."""

from __future__ import annotations

import uuid
from datetime import UTC
from decimal import Decimal
from typing import Any

from claim_contract.enums import DecisionOutcome
from claim_contract.models import Decision as ContractDecision
from claim_contract.models import Deduction, Money
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.core import ClaimCase, Decision


def to_contract(d: Decision) -> ContractDecision:
    deds = []
    for x in d.deductions or []:
        amt = x["amount"]["amount"] if isinstance(x["amount"], dict) else x["amount"]
        deds.append(Deduction(line_ref=str(x.get("line_ref", "manual")), rule_id=str(x["rule_id"]), amount=Money(amount=Decimal(str(amt))), explanation=str(x.get("explanation", ""))[:500]))
    reviewers = list(dict.fromkeys(d.reviewer_ids or [d.created_by]))[:3]
    calc_id = d.calc_result_id or uuid.UUID(int=0)
    return ContractDecision(
        outcome=DecisionOutcome(d.outcome), approved_amount=Money(amount=d.approved_amount or Decimal("0")), deductions=deds, reason_codes=list(d.reason_codes or []),
        reviewer_ids=reviewers, calc_trace_id=calc_id, policy_version=int((d.config_versions or {}).get("policy_rules") or 1),
        decided_at=(d.finalised_at or d.created_at).astimezone(UTC),
    )


async def contract_decision(session: AsyncSession, case: ClaimCase) -> ContractDecision | None:
    d = (await session.execute(select(Decision).where(Decision.case_id == case.id, Decision.kind == "final", Decision.status == "finalised")
                               .order_by(Decision.created_at.desc()).limit(1))).scalar_one_or_none()
    return to_contract(d) if d else None


def version_stamp(config_versions: dict[str, Any], engine_version: str | None) -> dict[str, Any]:
    """01-03 §5: policy_rules_v, thresholds_v, query_policy_v, doc_requirements_v, calc_engine_version."""
    out = dict(config_versions)
    out["calc_engine_version"] = engine_version
    for k in ("policy_rules", "thresholds", "query_policy", "doc_requirements"):
        out[f"{k}_v"] = config_versions.get(k)
    return out
