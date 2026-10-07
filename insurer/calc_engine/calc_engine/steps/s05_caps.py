"""S5 — per-group caps and pre/post-hospitalisation windows."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from ..models import CalcInput, FlagCode, MappedGroup
from ..money import allocate_cents, frac
from ..state import EngineState


def s05_caps(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    affected: list[str] = []
    for group, cap in sorted(inp.rules.line_caps.items()):
        members = [ls for ls in st.lines if ls.line.mapped_group.value == group and ls.allowed > 0]
        total = sum((ls.allowed for ls in members), Decimal("0"))
        if total > cap:
            exact: dict[str, Fraction] = {ls.ref: frac(ls.allowed) * frac(cap) / frac(total) for ls in members}
            new = allocate_cents(cap, exact)
            for ls in members:
                st.cut(ls, ls.allowed - new[ls.ref], "R-CAP-01", "S5", f"{group} capped at {cap}")
                affected.append(ls.ref)
    win, adm = inp.rules.hospitalisation_windows, inp.admission
    for ls in st.lines:
        g = ls.line.mapped_group
        if g not in (MappedGroup.pre_hospitalisation, MappedGroup.post_hospitalisation) or ls.allowed <= 0:
            continue
        d = ls.line.service_date
        if d is None:
            st.flag(FlagCode.NO_DATE_ON_LINE, "no service_date; allowed pending review", ls.ref)
        elif g == MappedGroup.pre_hospitalisation and (adm.admitted_on - d).days > win.pre_days:
            st.disallow_line(ls, "R-CAP-02", "S5", f"{(adm.admitted_on - d).days}d before admission (window {win.pre_days})")
            affected.append(ls.ref)
        elif g == MappedGroup.post_hospitalisation and (d - adm.discharged_on).days > win.post_days:
            st.disallow_line(ls, "R-CAP-03", "S5", f"{(d - adm.discharged_on).days}d after discharge (window {win.post_days})")
            affected.append(ls.ref)
    st.record("S5", "Per-line caps and hospitalisation windows", before, "R-CAP-0x" if affected else None, affected)
    return st
