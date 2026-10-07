"""S3 — procedure-group sub-limit scaling (largest-remainder allocation)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from ..models import CalcInput
from ..money import allocate_cents, frac
from ..state import EngineState


def s03_sublimit(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    affected: list[str] = []
    for g, limit in sorted(inp.rules.sub_limits.items()):
        members = [ls for ls in st.lines if ls.line.procedure_group == g and ls.allowed > 0]
        sum_g = sum((ls.allowed for ls in members), Decimal("0"))
        if sum_g > limit and sum_g > 0:
            exact: dict[str, Fraction] = {ls.ref: frac(ls.allowed) * frac(limit) / frac(sum_g) for ls in members}
            new = allocate_cents(limit, exact)
            for ls in members:
                st.cut(ls, ls.allowed - new[ls.ref], "R-SUB-01", "S3", f"Sub-limit {limit} for {g} (group total {sum_g})")
                affected.append(ls.ref)
    st.record("S3", "Procedure sub-limits", before, "R-SUB-01" if affected else None, affected)
    return st
