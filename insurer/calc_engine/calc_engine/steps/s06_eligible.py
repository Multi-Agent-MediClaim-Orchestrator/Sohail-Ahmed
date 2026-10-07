"""S6 — eligible subtotal."""

from __future__ import annotations

from ..models import BlockedReason, CalcInput
from ..money import ZERO
from ..state import EngineState


def s06_eligible(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    st.eligible_total = sum((ls.allowed for ls in st.lines), ZERO)
    if st.eligible_total == 0 and st.blocked is None:
        st.blocked = BlockedReason.ALL_LINES_EXCLUDED
    st.record("S6", "Eligible subtotal", before)
    return st
