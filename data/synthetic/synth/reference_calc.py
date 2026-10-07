"""Independent reference calculator for the insurer-side payable (Decimal, 2 dp, half-up).

Order of operations (provisional; Dev B's engine is the authority once it exists):
 1. gates: policy in force on the admission date; waiting period for the procedure (days since policy start);
 2. excluded categories are removed;
 3. room rent is capped per day (excess is a deduction; no proportionate deduction);
 4. deductible is taken off what is left, then co-pay % of the remainder, then the sum insured available caps it.
Routing: any failed gate -> human; payable <= t_auto -> auto-approve; > t_four -> two humans; else one human."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

Q = Decimal("0.01")


def money(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Q, ROUND_HALF_UP)


@dataclass
class Policy:
    start: dt.date
    end: dt.date
    sum_insured: Decimal
    room_cap_per_day: Decimal | None = None
    copay_pct: Decimal = Decimal(0)
    deductible: Decimal = Decimal(0)
    waiting_days: dict[str, int] = field(default_factory=dict)  # procedure code -> days
    excluded: set[str] = field(default_factory=set)  # line categories
    used: Decimal = Decimal(0)  # sum insured already consumed


@dataclass
class Result:
    payable: Decimal
    deductions: dict[str, Decimal]
    gates: dict[str, bool]
    route: str  # auto | one_human | two_humans


def calculate(
    policy: Policy,
    lines: list[dict[str, Any]],
    admitted: dt.date,
    days: int,
    procedure: str | None = None,
    t_auto: Decimal = Decimal(50000),
    t_four: Decimal = Decimal(500000),
) -> Result:
    gates = {
        "in_force": policy.start <= admitted <= policy.end,
        "waiting_period": procedure is None
        or (admitted - policy.start).days >= policy.waiting_days.get(procedure, 0),
    }
    ded: dict[str, Decimal] = {}
    total = sum((money(x["amount"]) for x in lines), Decimal(0))
    excluded = sum(
        (money(x["amount"]) for x in lines if x["category"] in policy.excluded), Decimal(0)
    )
    ded["excluded"] = excluded
    admissible = total - excluded
    rent = sum((money(x["amount"]) for x in lines if x["category"] == "room"), Decimal(0))
    if policy.room_cap_per_day is not None and "room" not in policy.excluded:
        allowed = min(rent, money(policy.room_cap_per_day) * days)
        ded["room_cap"] = rent - allowed
        admissible -= rent - allowed
    ded.setdefault("room_cap", Decimal(0))
    after_ded = max(Decimal(0), admissible - policy.deductible)
    ded["deductible"] = admissible - after_ded
    payable = money(after_ded * (1 - policy.copay_pct / 100))
    ded["copay"] = after_ded - payable
    room_left = max(Decimal(0), policy.sum_insured - policy.used)
    ded["sum_insured"] = max(Decimal(0), payable - room_left)
    payable = min(payable, room_left)
    if not all(gates.values()):
        payable, route = Decimal(0), "one_human"
    elif payable > t_four:
        route = "two_humans"
    elif payable <= t_auto:
        route = "auto"
    else:
        route = "one_human"
    return Result(payable, ded, gates, route)
