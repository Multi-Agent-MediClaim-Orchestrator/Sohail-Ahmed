"""S7 — per-claim deductible, allocated over lines proportionally to their current net."""

from __future__ import annotations

from fractions import Fraction

from ..models import CalcInput
from ..money import allocate_cents, frac
from ..state import EngineState, Hit


def s07_deductible(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    base = st.net_total()
    ded = min(inp.rules.deductible.amount, base)
    affected: list[str] = []
    if ded > 0 and base > 0:
        exact: dict[str, Fraction] = {ls.ref: frac(ded) * frac(ls.net) / frac(base) for ls in st.lines}
        alloc = allocate_cents(ded, exact)
        for ls in st.lines:
            a = alloc[ls.ref]
            if a > 0:
                ls.ded += a
                ls.hits.append(Hit("R-DED-01", "S7", a, f"Per-claim deductible {ded}"))
                affected.append(ls.ref)
    st.record("S7", "Deductible", before, "R-DED-01" if affected else None, affected)
    return st
