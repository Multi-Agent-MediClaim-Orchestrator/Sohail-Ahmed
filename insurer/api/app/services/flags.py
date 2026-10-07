"""Review-flag collection (03-04 §3.1). Pure given its inputs; each flag individually toggleable via ``thresholds.review_flags``."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..verification.schemas import Finding

ALL_FLAGS = ["overridden_blocker", "agent_deterministic_disagreement", "degraded_mode", "watchlist_hospital", "high_utilisation", "fraud_warning",
             "manual_increase", "escalated_case"]


@dataclass(frozen=True)
class FlagInputs:
    findings: list[Finding]
    degraded: bool
    manual_verification: bool
    watchlist_hospital: bool
    utilised: Decimal
    payable: Decimal
    sum_insured_total: Decimal  # sum insured + bonus
    approved_amount: Decimal  # what the reviewer is submitting
    escalated: bool = False


def collect_review_flags(i: FlagInputs, enabled: list[str], high_utilisation_pct: int = 80) -> list[str]:
    out: set[str] = set()
    if any(f.overridden and f.severity.value == "blocker" for f in i.findings):
        out.add("overridden_blocker")
    if any(f.code == "agent.disagrees_with_rules" for f in i.findings):
        out.add("agent_deterministic_disagreement")
    if i.degraded or i.manual_verification:
        out.add("degraded_mode")
    if i.watchlist_hospital:
        out.add("watchlist_hospital")
    if i.sum_insured_total > 0 and (i.utilised + i.approved_amount) * 100 > i.sum_insured_total * high_utilisation_pct:
        out.add("high_utilisation")
    if any(f.code == "auth.fraud_warning" and not f.overridden for f in i.findings):
        out.add("fraud_warning")
    if i.approved_amount > i.payable:
        out.add("manual_increase")
    if i.escalated:
        out.add("escalated_case")
    return sorted(out & set(enabled))
