"""S1 — waiting periods and pre-existing disease (whole claim or group blocks)."""

from __future__ import annotations

from ..models import AdmissionType, BlockedReason, CalcInput, FlagCode
from ..state import EngineState


def _block_groups(st: EngineState, groups: list[str], rule: str, why: str) -> list[str]:
    hit: list[str] = []
    for ls in st.lines:
        if ls.line.procedure_group in groups and ls.allowed > 0:
            st.disallow_line(ls, rule, "S1", why)
            hit.append(ls.ref)
    return hit


def s01_waiting(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    r, adm, m = inp.rules, inp.admission, inp.member
    days_cover = (adm.admitted_on - m.cover_start).days
    dx = adm.diagnosis_codes
    accident = adm.admission_type == AdmissionType.emergency and any(
        c.startswith(pfx) for c in dx for pfx in r.accident_icd_prefixes
    )
    rule: str | None = None
    affected: list[str] = []
    if days_cover < r.waiting_periods_days.initial and not accident:
        rule = "R-WAIT-01"
        st.block(BlockedReason.INITIAL_WAITING, rule, "S1",
                 f"Initial waiting period {r.waiting_periods_days.initial}d not completed ({days_cover}d of cover)")
    if st.blocked is None:
        for pe in m.pre_existing:
            if any(c.startswith(pe.icd_prefix) for c in dx) and days_cover < r.waiting_periods_days.pre_existing:
                rule = "R-WAIT-02"
                groups = r.pre_existing_group_map.get(pe.icd_prefix)
                why = f"Pre-existing {pe.icd_prefix}: {days_cover}d of {r.waiting_periods_days.pre_existing}d waiting"
                if groups:
                    affected += _block_groups(st, groups, rule, why)
                    st.flag(FlagCode.WAITING_PERIOD_HIT, f"{pe.icd_prefix} blocks groups {sorted(groups)}")
                else:
                    st.block(BlockedReason.PRE_EXISTING_WAITING, rule, "S1", why)  # unknown group => whole claim
                    break
        if st.blocked is None:
            for g, d in sorted(r.waiting_periods_days.specific.items()):
                if adm.procedure_group == g and days_cover < d:
                    rule = "R-WAIT-03"
                    affected += _block_groups(st, [g], rule, f"Specific-illness waiting {d}d for {g} ({days_cover}d of cover)")
                    st.flag(FlagCode.WAITING_PERIOD_HIT, f"specific waiting for {g}")
    st.record("S1", "Waiting periods and pre-existing disease", before, rule, affected)
    return st
