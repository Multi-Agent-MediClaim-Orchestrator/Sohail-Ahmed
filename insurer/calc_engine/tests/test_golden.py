import json
from decimal import Decimal
from pathlib import Path

import pytest
from calc_engine.engine import run
from calc_engine.models import CalcInput

GOLD = sorted((Path(__file__).parent / "golden").glob("*.json"))


@pytest.mark.parametrize("path", GOLD, ids=[p.stem for p in GOLD])
def test_golden(path):
    g = json.loads(path.read_text(encoding="utf-8"))
    res = run(CalcInput.model_validate(g["input"]))
    exp = g["expected"]
    assert f"{res.payable_total:.2f}" == exp["payable_total"], res.flag_codes()
    assert f"{res.patient_pays_total:.2f}" == exp["patient_pays_total"]
    assert (res.blocked.value if res.blocked else None) == exp["blocked"]
    for code in exp["flags"]:
        assert code in res.flag_codes(), (code, res.flag_codes())
    by_ref = {ln.line_ref: ln for ln in res.lines}
    for ref, amount in exp["line_payables"].items():
        assert f"{by_ref[ref].payable:.2f}" == amount, ref
    for ref, amount in exp.get("line_allowed", {}).items():
        assert f"{by_ref[ref].allowed_after_caps:.2f}" == amount, ref
    if exp["remaining_after"] is not None:
        assert f"{res.remaining_sum_insured_after:.2f}" == exp["remaining_after"]
    # invariants visible in the output
    assert sum(ln.payable for ln in res.lines) == res.payable_total
    assert sum(d.amount.amount for d in res.summary_deductions) == res.patient_pays_total
    for ln in res.lines:
        assert ln.claimed == ln.payable + ln.disallowed + ln.deductible_share + ln.co_pay_share + ln.sum_insured_cut


def test_golden_set_is_complete():
    names = {p.stem for p in GOLD}
    expected = {"ex01", "ex02", "ex03", "ex04a", "ex04b", "ex05a", "ex05b", "ex06", "ex07a", "ex07b", "ex08", "ex09",
                "ex10", "ex11a", "ex11b", "ex12", "ex13", "ex14", "ex15a", "ex15b"} | {f"edge{i:02d}" for i in range(1, 11)}
    assert expected <= names


def test_deterministic_bytes():
    g = json.loads((Path(__file__).parent / "golden" / "ex14.json").read_text(encoding="utf-8"))
    a = run(CalcInput.model_validate(g["input"])).model_dump_json()
    b = run(CalcInput.model_validate(g["input"])).model_dump_json()
    assert a == b
    assert Decimal(json.loads(a)["payable_total"]) == Decimal("128800.00")
