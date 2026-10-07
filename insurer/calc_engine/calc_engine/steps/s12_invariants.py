"""S12 — invariants (07 §2.3). A violation raises ``EngineInvariantError`` (API: 500 with trace)."""

from __future__ import annotations

from decimal import Decimal

from ..errors import EngineInvariantError
from ..models import CalcInput
from ..money import ZERO
from ..state import EngineState


def s12_invariants(st: EngineState, inp: CalcInput) -> None:
    claimed = sum((ls.claimed for ls in st.lines), ZERO)
    payable = sum((ls.net for ls in st.lines), ZERO)

    def fail(msg: str) -> None:
        raise EngineInvariantError(msg, [t.model_dump(mode="json") for t in st.trace])

    if not (ZERO <= payable <= claimed):
        fail(f"payable {payable} outside [0, {claimed}]")
    if payable > st.remaining_before:
        fail("payable exceeds remaining sum insured")
    for ls in st.lines:
        if ls.allowed < 0 or ls.allowed > ls.claimed:
            fail(f"{ls.ref}: allowed out of range")
        if ls.net < 0:
            fail(f"{ls.ref}: negative payable")
        disallowed = ls.claimed - ls.allowed
        if ls.claimed != ls.net + disallowed + ls.ded + ls.copay + ls.sicut:  # pragma: no cover - a tautology while `net` is derived; guards a future refactor of LineState.net
            fail(f"{ls.ref}: line identity broken")
        for v in (ls.allowed, ls.ded, ls.copay, ls.sicut):
            if isinstance(v, Decimal) and v.as_tuple().exponent < -2:  # type: ignore[operator]
                fail(f"{ls.ref}: sub-cent amount")
        hits_total = sum((h.amount for h in ls.hits), ZERO)
        if hits_total != ls.claimed - ls.net:
            fail(f"{ls.ref}: deductions {hits_total} != claimed - payable {ls.claimed - ls.net}")
    if st.blocked is not None and st.blocked.value in (
        "POLICY_INACTIVE", "PREMIUM_LAPSED", "OUTSIDE_COVER", "INITIAL_WAITING", "PRE_EXISTING_WAITING",
        "ALL_LINES_EXCLUDED", "SUM_INSURED_EXHAUSTED",
    ) and payable != 0:
        fail("blocked claim has non-zero payable")
