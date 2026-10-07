"""S11 — final per-line payable. Money movements were already allocated with largest remainder at S3-S10."""

from __future__ import annotations

from ..models import CalcInput
from ..state import EngineState
from .s10_sum_insured import remaining_sum_insured


def s11_allocate(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    if not any(t.step == "S10" for t in st.trace):  # claim blocked before S10: nothing consumed
        st.remaining_before = remaining_sum_insured(inp)
    st.remaining_after = st.remaining_before - st.net_total()
    st.record("S11", "Allocation to lines", before)
    return st
