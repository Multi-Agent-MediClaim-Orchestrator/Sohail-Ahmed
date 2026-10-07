"""S4 — room-rent / ICU eligibility and proportionate deduction (single applied ratio, decision D-4)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from ..models import CalcInput, FlagCode, MappedGroup
from ..money import allocate_cents, frac, q
from ..state import EngineState


def _eligible_rate(inp: CalcInput, kind: str) -> Decimal:
    rr = inp.rules.room_rent
    pct = rr.percent if kind == "room" else rr.icu_percent
    caps = [q(frac(inp.policy.sum_insured) * frac(pct) / 100)]
    if rr.per_day_cap is not None:
        caps.append(rr.per_day_cap)
    tier = inp.admission.hospital.room_rent_tier
    if rr.use_tier_caps and tier and tier in rr.tier_caps:
        caps.append(rr.tier_caps[tier])
    return min(caps)


def s04_room(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    adm = inp.admission
    day_care = adm.day_care or (adm.procedure_group in inp.rules.day_care_groups)
    affected: list[str] = []
    ratios: dict[str, Fraction] = {}
    if not day_care:
        for ls in st.lines:
            ln = ls.line
            if ln.mapped_group not in (MappedGroup.room_rent, MappedGroup.icu) or ls.allowed <= 0:
                continue
            kind = "icu" if ln.mapped_group == MappedGroup.icu else "room"
            cap = _eligible_rate(inp, kind)
            rate = ln.unit_price
            if ln.days is None:
                days = max(1, (adm.discharged_on - adm.admitted_on).days)
                st.flag(FlagCode.ROOM_DAYS_ASSUMED, f"days assumed = {days}", ln.line_ref)
            else:
                days = ln.days
            if rate > cap:
                new_allowed = min(ls.allowed, q(cap * days))
                st.cut(
                    ls,
                    ls.allowed - new_allowed,
                    "R-ROOM-02" if kind == "icu" else "R-ROOM-01",
                    "S4",
                    f"Rate {rate} above eligible {cap} for {days} day(s)",
                )
                ratios[kind] = min(ratios.get(kind, Fraction(1)), frac(cap) / frac(rate))
                affected.append(ls.ref)
        if inp.rules.proportionate_deduction and ratios:
            applied = min(ratios.values())
            rules = inp.rules
            targets = [
                ls
                for ls in st.lines
                if ls.allowed > 0
                and ls.line.mapped_group.value in rules.proportionate_applies_to
                and ls.line.mapped_group.value not in rules.proportionate_exempt
            ]
            if targets:
                exact = {ls.ref: frac(ls.allowed) * applied for ls in targets}
                new = allocate_cents(sum(exact.values(), Fraction(0)), exact)
                for ls in targets:
                    st.cut(ls, ls.allowed - new[ls.ref], "R-PROP-01", "S4", f"Proportionate deduction, ratio {applied.numerator}/{applied.denominator}")
                    affected.append(ls.ref)
    st.record("S4", "Room rent eligibility and proportionate deduction", before, "R-ROOM-01" if affected else None, affected)
    return st
