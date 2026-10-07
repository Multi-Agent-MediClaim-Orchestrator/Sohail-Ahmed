"""S0 — whole-claim blocks: policy status, premium, period, cover start."""

from __future__ import annotations

from ..models import BlockedReason, CalcInput, FlagCode
from ..state import EngineState


def s00_prechecks(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    p, m, adm = inp.policy, inp.member, inp.admission.admitted_on
    rule = None
    if p.status != "active":
        rule = "R-BLK-01"
        st.block(BlockedReason.POLICY_INACTIVE, rule, "S0", f"Policy status is {p.status}")
    elif p.premium_paid_until < adm:
        gap = (adm - p.premium_paid_until).days
        if gap > p.grace_days:
            rule = "R-BLK-02"
            st.block(BlockedReason.PREMIUM_LAPSED, rule, "S0", f"Premium unpaid {gap} days (grace {p.grace_days})")
        else:
            st.flag(FlagCode.PREMIUM_IN_GRACE, f"premium overdue {gap} days, within {p.grace_days}-day grace")
    if st.blocked is None and (not (p.start_date <= adm <= p.end_date) or adm < m.cover_start):
        rule = "R-BLK-03"
        st.block(BlockedReason.OUTSIDE_COVER, rule, "S0", "Admission outside policy period or before member cover start")
    st.record("S0", "Pre-checks (policy status, premium, period, cover start)", before, rule)
    return st
