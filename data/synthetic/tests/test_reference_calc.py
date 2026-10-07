"""Hand-checked rows for the reference calculator (expected values worked out on paper, not by the code)."""

import datetime as dt
from decimal import Decimal as D

import pytest
from synth.reference_calc import Policy, calculate

D0 = dt.date(2026, 1, 10)

# si, cap/day, copay%, deductible, used, room, days, other, excluded(cosmetic), expected payable
ROWS = [
    (500000, None, 0, 0, 0, 9000, 3, 21000, 0, "30000"),
    (500000, None, 10, 0, 0, 9000, 3, 21000, 0, "27000"),
    (500000, None, 20, 0, 0, 9000, 3, 21000, 0, "24000"),
    (500000, None, 0, 2000, 0, 9000, 3, 21000, 0, "28000"),
    (500000, None, 10, 2000, 0, 9000, 3, 21000, 0, "25200"),
    (500000, 2000, 0, 0, 0, 9000, 3, 21000, 0, "27000"),
    (500000, 3000, 0, 0, 0, 9000, 3, 21000, 0, "30000"),
    (500000, 2000, 10, 0, 0, 9000, 3, 21000, 0, "24300"),
    (500000, 2000, 10, 2000, 0, 9000, 3, 21000, 0, "22500"),
    (20000, None, 0, 0, 0, 9000, 3, 21000, 0, "20000"),
    (30000, None, 0, 0, 5000, 9000, 3, 21000, 0, "25000"),
    (30000, None, 0, 0, 30000, 9000, 3, 21000, 0, "0"),
    (100000, None, 25, 0, 0, 9000, 3, 21000, 0, "22500"),
    (500000, None, 0, 50000, 0, 9000, 3, 21000, 0, "0"),
    (500000, None, 0, 0, 0, 9000, 3, 16000, 5000, "25000"),
    (500000, None, 10, 0, 0, 9000, 3, 16000, 5000, "22500"),
    (500000, 2000, 0, 0, 0, 9000, 3, 16000, 5000, "22000"),
    (500000, 2000, 10, 2000, 0, 9000, 3, 16000, 5000, "18000"),
    (500000, 1000, 0, 0, 0, 0, 3, 10000, 0, "10000"),
    (500000, 3000, 0, 0, 0, 12000, 4, 8000, 0, "20000"),
    (500000, None, 10, 0, 0, 0, 1, 10001, 0, "9000.90"),
    (500000, None, 33, 0, 0, 0, 1, "1005.55", 0, "673.72"),
    (500000, None, 50, 0, 0, 0, 1, "0.05", 0, "0.03"),
    (500000, None, 0, 0, 0, 0, 1, 5000, 0, "5000"),
    (10_000_000, 10000, 0, 0, 0, 300000, 10, 700000, 0, "800000"),
]


def run(row, **kw):
    si, cap, cp, ded, used, room, days, other, excl, _ = row
    p = Policy(
        dt.date(2025, 1, 1),
        dt.date(2026, 12, 31),
        D(si),
        D(cap) if cap else None,
        D(cp),
        D(ded),
        used=D(used),
        excluded={"cosmetic"},
    )
    lines = [
        {"category": "room", "amount": D(str(room))},
        {"category": "other", "amount": D(str(other))},
        {"category": "cosmetic", "amount": D(excl)},
    ]
    return calculate(p, lines, D0, days, **kw)


@pytest.mark.parametrize("row", ROWS)
def test_payable(row):
    assert run(row).payable == D(row[-1])


def test_si_cap_recorded():
    assert run(ROWS[9]).payable == D("20000.00")
    assert run(ROWS[9]).deductions["sum_insured"] == D("10000.00")


def _policy(**kw):
    return Policy(dt.date(2025, 1, 1), dt.date(2026, 12, 31), D(10_000_000), **kw)


def _lines(total):
    return [{"category": "other", "amount": D(str(total))}]


def test_gates_and_routes():
    out = calculate(_policy(), _lines(1000), dt.date(2027, 1, 1), 1)
    assert (out.payable, out.route, out.gates["in_force"]) == (0, "one_human", False)
    p = _policy(waiting_days={"cataract": 730})
    out = calculate(p, _lines(1000), D0, 1, "cataract")
    assert (out.payable, out.route, out.gates["waiting_period"]) == (0, "one_human", False)
    assert calculate(p, _lines(1000), D0, 1, "fracture").route == "auto"
    assert calculate(_policy(), _lines(50000), D0, 1).route == "auto"
    assert calculate(_policy(), _lines("50000.01"), D0, 1).route == "one_human"
    assert calculate(_policy(), _lines(500000), D0, 1).route == "one_human"
    assert calculate(_policy(), _lines("500000.01"), D0, 1).route == "two_humans"
