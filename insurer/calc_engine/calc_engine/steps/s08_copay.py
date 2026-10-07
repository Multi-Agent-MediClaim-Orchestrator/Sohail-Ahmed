"""S8 — co-pay: age condition and non-network, non-stacking by default (higher wins)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

from ..models import CalcInput, FlagCode
from ..money import allocate_cents, frac, pct
from ..state import EngineState, Hit


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def s08_copay(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    r = inp.rules
    cands: list[tuple[Decimal, str]] = []
    age = age_on(inp.member.dob, inp.admission.admitted_on)
    cond = r.co_pay.conditions.age_gte
    if r.co_pay.percent > 0 and (cond is None or age >= cond):
        cands.append((r.co_pay.percent, "R-COPAY-01"))
    if inp.admission.hospital.network_status == "non_network" and r.non_network_co_pay_percent > 0:
        cands.append((r.non_network_co_pay_percent, "R-COPAY-02"))
    affected: list[str] = []
    rule_id: str | None = None
    if cands:
        if r.stack_co_pay and len(cands) > 1:
            cp = min(sum((c[0] for c in cands), Decimal("0")), Decimal("100"))
            rule_id = "R-COPAY-03"
        else:
            cp, rule_id = max(cands, key=lambda c: c[0])
        st.flag(FlagCode.CO_PAY_SELECTED, f"{rule_id}: {cp}%")
        st.copay_rule = rule_id
        base = st.net_total()
        amount = pct(base, cp)
        if amount > 0 and base > 0:
            exact: dict[str, Fraction] = {ls.ref: frac(amount) * frac(ls.net) / frac(base) for ls in st.lines}
            alloc = allocate_cents(amount, exact)
            for ls in st.lines:
                a = alloc[ls.ref]
                if a > 0:
                    ls.copay += a
                    ls.hits.append(Hit(rule_id, "S8", a, f"Co-pay {cp}%"))
                    affected.append(ls.ref)
    st.record("S8", "Co-pay", before, rule_id, affected)
    return st
