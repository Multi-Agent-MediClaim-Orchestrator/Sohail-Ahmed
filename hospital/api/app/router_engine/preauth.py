"""Simulated pre-auth check (doc 05 §5.4). Never blocks; the real insurer is authoritative."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass
class PreauthResult:
    state: str  # ok | issues | not_found | not_applicable | missing
    issues: list[str] = field(default_factory=list)
    warn: str | None = None


def preauth_check(
    row: Any, member_id: str, admitted_on: date | None, expected_amount: Decimal | None
) -> PreauthResult:
    if row is None:
        return PreauthResult("not_found", warn="Pre-auth reference not found")
    issues: list[str] = []
    if row.member_id != member_id:
        issues.append("member_mismatch")
    if row.status != "approved" and row.status != "enhanced":
        issues.append(f"status_{row.status}")
    if admitted_on and not (row.valid_from <= admitted_on <= row.valid_to):
        issues.append("outside_validity")
    if expected_amount is not None and expected_amount > row.approved_amount * Decimal("1.10"):
        issues.append("expected_exceeds_preauth")
    return PreauthResult(
        "ok" if not issues else "issues",
        issues=issues,
        warn=None if not issues else "Pre-auth has issues: " + ", ".join(issues),
    )
