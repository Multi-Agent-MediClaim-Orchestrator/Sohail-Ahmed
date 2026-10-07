"""S10 — sum-insured cap. Exhaustion is a normal blocked result, not an error."""

from __future__ import annotations

from fractions import Fraction

from ..models import BlockedReason, CalcInput, FlagCode
from ..money import ZERO, allocate_cents, frac
from ..state import EngineState, Hit


def remaining_sum_insured(inp: CalcInput):
    p = inp.policy
    return max(ZERO, p.sum_insured + p.bonus_sum - p.utilised_this_year)


def s10_sum_insured(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    st.remaining_before = remaining_sum_insured(inp)
    pre = st.net_total()
    payable = min(pre, st.remaining_before)
    cut = pre - payable
    affected: list[str] = []
    rule_id: str | None = None
    if cut > 0:
        rule_id = "R-SI-02" if st.remaining_before == 0 else "R-SI-01"
        exact: dict[str, Fraction] = {ls.ref: frac(cut) * frac(ls.net) / frac(pre) for ls in st.lines}
        alloc = allocate_cents(cut, exact)
        for ls in st.lines:
            a = alloc[ls.ref]
            if a > 0:
                ls.sicut += a
                ls.hits.append(Hit(rule_id, "S10", a, f"Sum insured remaining {st.remaining_before}"))
                affected.append(ls.ref)
        if st.remaining_before == 0:
            st.blocked = BlockedReason.SUM_INSURED_EXHAUSTED
        else:
            st.flag(FlagCode.SUM_INSURED_CAPPED, f"payable capped to remaining sum insured {st.remaining_before}")
    st.record("S10", "Sum insured cap", before, rule_id, affected)
    return st
