import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from builders import ADMITTED, line, make_input, rules
from calc_engine import mapping
from calc_engine.engine import MAX_LINES, run
from calc_engine.errors import EngineInvariantError, RulesInvalidError, TooManyLinesError
from calc_engine.explain import explain
from calc_engine.models import CalcInput
from calc_engine.money import allocate_cents, pct, q
from calc_engine.rules_schema import validate_rules
from calc_engine.state import EngineState
from calc_engine.steps.s08_copay import age_on
from calc_engine.steps.s12_invariants import s12_invariants


def go(lines, **kw):
    return run(CalcInput.model_validate(make_input(lines, **kw)))


# ------------------------------------------------------------------ money
@pytest.mark.parametrize("x,expected", [("0.005", "0.01"), ("0.004", "0.00"), ("2.675", "2.68"), ("10.005", "10.01"), ("1.994", "1.99")])
def test_q_half_up_decimal(x, expected):
    assert q(Decimal(x)) == Decimal(expected)


@pytest.mark.parametrize("x,expected", [(Fraction(1, 200), "0.01"), (Fraction(1, 300), "0.00"), (Fraction(5, 3), "1.67"), (Fraction(-1, 200), "-0.01")])
def test_q_half_up_fraction(x, expected):
    assert q(x) == Decimal(expected)


def test_pct():
    assert pct(Decimal("100.05"), Decimal("10")) == Decimal("10.01")  # 10.005 -> half up


def test_allocate_tie_break_by_line_ref_and_one_cent_drift():
    out = allocate_cents(Decimal("0.02"), {"B": Fraction(1, 300), "A": Fraction(1, 300), "C": Fraction(1, 300)})
    assert sum(out.values()) == Decimal("0.02") and out["A"] == Decimal("0.01") and out["B"] == Decimal("0.01") and out["C"] == 0
    assert allocate_cents(Decimal("5"), {}) == {}
    with pytest.raises(EngineInvariantError, match="allocation_drift"):
        allocate_cents(Decimal("10"), {"A": Fraction(1)})


# ------------------------------------------------------------------ models / rules validation
def test_float_and_three_decimals_rejected():
    d = make_input([line("L1", "medicine", "10.00")])
    d["lines"][0]["claimed_amount"] = 10.0
    with pytest.raises(ValueError):
        CalcInput.model_validate(d)
    d["lines"][0]["claimed_amount"] = "10.005"
    with pytest.raises(ValueError):
        CalcInput.model_validate(d)
    d["lines"][0]["claimed_amount"] = "-1.00"
    with pytest.raises(ValueError):
        CalcInput.model_validate(d)


def test_duplicate_line_ref_and_dates():
    d = make_input([line("L1", "medicine", "10.00"), line("L1", "medicine", "10.00")])
    with pytest.raises(ValueError):
        CalcInput.model_validate(d)
    d = make_input([line("L1", "medicine", "10.00")])
    d["admission"]["discharged_on"] = (ADMITTED - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        CalcInput.model_validate(d)


def test_rules_schema_strictness():
    base = rules()
    assert validate_rules(base).schema_version == 1
    for key in ("room_rent", "waiting_periods_days", "exclusions"):
        bad = {k: v for k, v in base.items() if k != key}
        with pytest.raises(RulesInvalidError):
            validate_rules(bad)
    with pytest.raises(RulesInvalidError):
        validate_rules({**base, "surprise": 1})
    with pytest.raises(RulesInvalidError):
        validate_rules({**base, "co_pay": {"percent": "150"}})
    with pytest.raises(RulesInvalidError):
        validate_rules({**base, "co_pay": {"percent": 10.5}})
    with pytest.raises(RulesInvalidError):
        validate_rules({**base, "deductible": {"amount": "5", "type": "per_year"}})
    with pytest.raises(RulesInvalidError):
        validate_rules({**base, "proportionate_exempt": ["doctor_fees"]})


def test_too_many_lines(monkeypatch):
    monkeypatch.setattr("calc_engine.engine.MAX_LINES", 2)
    with pytest.raises(TooManyLinesError):
        go([line(f"L{i}", "medicine", "1.00") for i in range(3)])
    assert MAX_LINES >= 1


# ------------------------------------------------------------------ step edge tables
def test_s0_boundaries():
    # admission on policy start / end dates is covered; day before start is not
    lines = [line("L1", "medicine", "1000.00")]
    d = make_input(lines)
    d["policy"]["start_date"] = ADMITTED.isoformat()
    assert run(CalcInput.model_validate(d)).blocked is None
    d["policy"]["start_date"] = (ADMITTED + timedelta(days=1)).isoformat()
    assert run(CalcInput.model_validate(d)).blocked.value == "OUTSIDE_COVER"
    d = make_input(lines)
    d["policy"]["end_date"] = ADMITTED.isoformat()  # discharge after end date is allowed
    assert run(CalcInput.model_validate(d)).blocked is None


@pytest.mark.parametrize("gap,blocked", [(30, None), (31, "PREMIUM_LAPSED")])
def test_s0_grace_boundary(gap, blocked):
    r = go([line("L1", "medicine", "1000.00")], premium_until=(ADMITTED - timedelta(days=gap)).isoformat())
    assert (r.blocked.value if r.blocked else None) == blocked


@pytest.mark.parametrize("cover_days,blocked", [(29, "INITIAL_WAITING"), (30, None)])
def test_s1_initial_boundary(cover_days, blocked):
    r = go([line("L1", "medicine", "1000.00")], cover_start=(ADMITTED - timedelta(days=cover_days)).isoformat())
    assert (r.blocked.value if r.blocked else None) == blocked


def test_s1_specific_waiting_blocks_group_only():
    r = go([line("L1", "surgeon_fees", "5000.00", procedure_group="hernia"), line("L2", "medicine", "1000.00")],
           procedure_group="hernia", cover_start=(ADMITTED - timedelta(days=100)).isoformat(),
           rules_ov={"waiting_periods_days": {"specific": {"hernia": 365}}})
    assert r.payable_total == Decimal("1000.00") and "WAITING_PERIOD_HIT" in r.flag_codes()
    r = go([line("L1", "surgeon_fees", "5000.00", procedure_group="hernia")], procedure_group="hernia",
           cover_start=(ADMITTED - timedelta(days=365)).isoformat(), rules_ov={"waiting_periods_days": {"specific": {"hernia": 365}}})
    assert r.payable_total == Decimal("5000.00")


def test_s2_icd_exclusion_and_non_medical_policies():
    r = go([line("L1", "surgeon_fees", "5000.00")], dx=["Z41.1"], rules_ov={"exclusions": {"icd_prefixes": ["Z41"]}})
    assert r.blocked.value == "ALL_LINES_EXCLUDED" and r.lines[0].rule_trace[0].rule_id == "R-EXCL-01"
    nm = [line("L1", "non_medical", "500.00", is_non_medical=True), line("L2", "medicine", "500.00")]
    assert go(nm).payable_total == Decimal("500.00")
    assert go(nm, rules_ov={"exclusions": {"non_medical_policy": "allow", "tags": []}}).payable_total == Decimal("1000.00")
    tagged = [line("L1", "non_medical", "500.00", exclusion_tags=["non_medical"]), line("L2", "medicine", "500.00")]
    r = go(tagged, rules_ov={"exclusions": {"non_medical_policy": "exclude_listed", "tags": []}})
    assert r.payable_total == Decimal("500.00") and r.lines[0].rule_trace[0].rule_id == "R-EXCL-03"


def test_s3_group_below_limit_untouched_and_empty_group():
    r = go([line("L1", "surgeon_fees", "10000.00", procedure_group="knee")], rules_ov={"sub_limits": {"knee": "20000", "other": "1"}})
    assert r.payable_total == Decimal("10000.00")


def test_s4_tier_and_per_day_caps_and_proportionate_off():
    room = [line("L1", "room_rent", "20000.00", qty="4", unit_price="5000.00", days=4), line("L2", "surgeon_fees", "10000.00")]
    r = go(room, days=4, rules_ov={"room_rent": {"per_day_cap": "4000"}})
    assert r.payable_total == Decimal("16000.00") + Decimal("10000.00") * Decimal("0.8")
    r = go(room, days=4, rules_ov={"room_rent": {"use_tier_caps": True}}, )
    assert r.payable_total == Decimal("30000.00")  # tier None -> no tier cap
    d = make_input(room, days=4, rules_ov={"room_rent": {"use_tier_caps": True}})
    d["admission"]["hospital"]["room_rent_tier"] = "C"
    assert run(CalcInput.model_validate(d)).payable_total == Decimal("16000.00") + Decimal("8000.00")
    r = go(room, days=4, rules_ov={"room_rent": {"per_day_cap": "4000"}, "proportionate_deduction": False})
    assert r.payable_total == Decimal("26000.00")


def test_s4_day_care_skips_room_logic():
    r = go([line("L1", "room_rent", "20000.00", qty="1", unit_price="20000.00", days=1)], day_care=True)
    assert r.payable_total == Decimal("20000.00")


def test_s5_post_window_and_no_date():
    post = [line("L1", "post_hospitalisation", "1000.00", service_date=(ADMITTED + timedelta(days=3 + 61)).isoformat()),
            line("L2", "post_hospitalisation", "2000.00", service_date=(ADMITTED + timedelta(days=3 + 60)).isoformat())]
    r = go(post)
    assert r.payable_total == Decimal("2000.00") and r.lines[0].rule_trace[0].rule_id == "R-CAP-03"


def test_s7_deductible_larger_than_eligible_and_before_order():
    r = go([line("L1", "medicine", "500.00")], rules_ov={"deductible": {"amount": "1000"}})
    assert r.payable_total == 0 and r.blocked is None
    r = go([line("L1", "medicine", "500.00")], rules_ov={"deductible": {"amount": "1000"}, "co_pay_order": "before_deductible"},
           dob="1950-01-01")
    assert r.payable_total == 0


def test_s8_copay_candidates_and_age_boundary():
    one = [line("L1", "medicine", "1000.00")]
    assert age_on(date(1965, 9, 2), date(2026, 9, 1)) == 60 and age_on(date(1965, 9, 1), date(2026, 9, 1)) == 61
    assert go(one, dob="1965-09-02").payable_total == Decimal("1000.00")
    assert go(one, dob="1965-09-01").payable_total == Decimal("900.00")
    r = go(one, dob="1960-01-01", network="non_network", rules_ov={"stack_co_pay": True, "co_pay": {"percent": "90"}, "non_network_co_pay_percent": "40"})
    assert r.payable_total == 0 and "CO_PAY_SELECTED" in r.flag_codes()  # capped at 100%
    r = go(one, rules_ov={"co_pay": {"percent": "10", "conditions": {}}})
    assert r.payable_total == Decimal("900.00")  # unconditional co-pay
    r = go(one, network="non_network", rules_ov={"stack_co_pay": True})
    assert r.payable_total == Decimal("800.00")  # only one candidate with stacking on


def test_s10_exact_cap_and_utilisation():
    r = go([line("L1", "medicine", "1000.00")], si="100000.00", utilised="99000.00")
    assert r.payable_total == Decimal("1000.00") and "SUM_INSURED_CAPPED" not in r.flag_codes()
    r = go([line("L1", "medicine", "1000.00")], si="100000.00", utilised="99500.00")
    assert r.payable_total == Decimal("500.00") and r.remaining_sum_insured_after == 0


def test_zero_value_line_is_carried():
    r = go([line("L1", "medicine", "0.00"), line("L2", "medicine", "100.00")])
    assert r.lines[0].payable == 0 and r.payable_total == Decimal("100.00")


def test_summary_deductions_reconcile_and_explain_runs():
    path = Path(__file__).parents[1] / "golden" / "ex14.json"
    inp = CalcInput.model_validate(json.loads(path.read_text(encoding="utf-8"))["input"])
    r = run(inp)
    text = explain(r, str(inp.case_id))
    assert "PAYABLE 128,800.00" in text and "R-EXCL-02" in text
    assert all(d.rule_id.startswith("R-") for d in r.summary_deductions)


def test_invariant_violation_raises():
    st = EngineState.from_lines(CalcInput.model_validate(make_input([line("L1", "medicine", "100.00")])).lines)
    st.lines[0].ded = Decimal("5.00")  # a deduction without a recorded hit breaks the identity
    with pytest.raises(EngineInvariantError):
        s12_invariants(st, CalcInput.model_validate(make_input([line("L1", "medicine", "100.00")])))
    st = EngineState.from_lines(CalcInput.model_validate(make_input([line("L1", "medicine", "100.00")])).lines)
    st.lines[0].allowed = Decimal("500.00")
    with pytest.raises(EngineInvariantError):
        s12_invariants(st, CalcInput.model_validate(make_input([line("L1", "medicine", "100.00")])))


def test_flags_deduplicated_and_inputs_not_mutated():
    d = make_input([line("L1", "other", "100.00")])
    before = copy.deepcopy(d)
    run(CalcInput.model_validate(d))
    assert d == before


# ------------------------------------------------------------------ mapping
@pytest.mark.parametrize("cat,desc,group", [
    ("room", "Semi private room", "room_rent"), ("other", "Ambulance charges", "ambulance"), ("surgery", "Surgeon fee", "surgeon_fees"),
    ("medicine", "Inj. Ceftriaxone", "medicine"), ("consumable", "Surgical gloves", "consumable"), ("other", "Coronary stent", "implant"),
    ("other", "Registration charges", "non_medical"), ("investigation", "CBC", "investigation"), ("consultation", "Visit", "doctor_fees"),
    ("other", "OT charges", "ot_charges"), ("other", "Pre-hospitalisation tests", "pre_hospitalisation"),
])
def test_mapping_rules(cat, desc, group):
    m = mapping.map_line(cat, desc)
    assert m is not None and m.mapped_group.value == group


def test_mapping_unknown_returns_none_and_flags():
    assert mapping.map_line("other", "Miscellaneous thing") is None
    assert mapping.map_line("other", "Cosmetic hair transplant").tags == ("cosmetic",)
    assert mapping.map_line("other", "Stent").is_implant
