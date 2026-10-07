"""Targets the defensive branches the golden/property suites cannot reach (07 §9: 100% line+branch coverage)."""

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from builders import line, make_input
from calc_engine import __main__ as cli
from calc_engine.errors import EngineInvariantError
from calc_engine.models import CalcInput
from calc_engine.money import floor_cents, frac
from calc_engine.rules_schema import RoomRent
from calc_engine.state import EngineState
from calc_engine.steps.s12_invariants import s12_invariants
from pydantic import ValidationError

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def inp(lines=None):
    return CalcInput.model_validate(make_input(lines or [line("L1", "medicine", "100.00")]))


# ------------------------------------------------------------------ models: money/qty validators
@pytest.mark.parametrize("bad", [1.5, True, "abc", "NaN", "Infinity", "-1.00", "1.234"])
def test_claimed_amount_rejects_bad_values(bad):
    d = make_input([line("L1", "medicine", "100.00")])
    d["lines"][0]["claimed_amount"] = bad
    with pytest.raises(ValidationError):
        CalcInput.model_validate(d)


@pytest.mark.parametrize("bad", [1.5, True, "abc", "NaN", "-1"])
def test_qty_rejects_bad_values(bad):
    d = make_input([line("L1", "medicine", "100.00")])
    d["lines"][0]["qty"] = bad
    with pytest.raises(ValidationError):
        CalcInput.model_validate(d)


@pytest.mark.parametrize("path,bad", [("room_rent.percent", "-1"), ("room_rent.icu_percent", "-1")])
def test_rules_percent_must_be_non_negative(path, bad):
    with pytest.raises(ValidationError):
        RoomRent.model_validate({"type": "percent_of_si", "percent": bad, "icu_percent": "2.0"} if "icu" not in path else {"type": "percent_of_si", "percent": "1.0", "icu_percent": bad})


@pytest.mark.parametrize("bad", [1.5, True, "xyz", "NaN"])
def test_rules_decimal_fields_reject_bad_values(bad):
    d = make_input([line("L1", "medicine", "100.00")])
    d["rules"]["co_pay"]["percent"] = bad
    with pytest.raises(ValidationError):
        CalcInput.model_validate(d)


@pytest.mark.parametrize("bad", ["-5", "1.234"])
def test_rules_money_fields_reject_negative_and_sub_cent(bad):
    d = make_input([line("L1", "medicine", "100.00")])
    d["rules"]["deductible"]["amount"] = bad
    with pytest.raises(ValidationError):
        CalcInput.model_validate(d)


# ------------------------------------------------------------------ money helpers
def test_frac_accepts_fraction_decimal_int_and_floor_cents():
    assert frac(Fraction(1, 3)) == Fraction(1, 3) and frac(Decimal("1.5")) == Fraction(3, 2) and frac(2) == Fraction(2)
    assert floor_cents(Fraction(10, 3)) == Decimal("3.33") and floor_cents(Fraction(2, 3)) == Decimal("0.66")


# ------------------------------------------------------------------ state
def test_flag_dedupes_and_cut_clamps_and_ignores_non_positive():
    st = EngineState.from_lines(inp().lines)
    st.flag("PREMIUM_IN_GRACE", "m")  # type: ignore[arg-type]
    st.flag("PREMIUM_IN_GRACE", "m")  # type: ignore[arg-type]
    assert len(st.flags) == 1
    ls = st.lines[0]
    st.cut(ls, Decimal("0"), "R", "S", "noop")
    assert ls.allowed == Decimal("100.00") and not ls.hits
    st.cut(ls, Decimal("500.00"), "R", "S", "clamped to what is left")
    assert ls.allowed == Decimal("0.00") and ls.hits[0].amount == Decimal("100.00")


# ------------------------------------------------------------------ S12: every invariant can fire
def fresh():
    i = inp()
    st = EngineState.from_lines(i.lines)
    st.remaining_before = Decimal("1000.00")
    return st, i


def test_s12_each_violation_is_detected():
    from calc_engine.models import BlockedReason

    st, i = fresh()
    st.lines[0].allowed = Decimal("200.00")  # payable above claimed
    with pytest.raises(EngineInvariantError, match="outside"):
        s12_invariants(st, i)
    st, i = fresh()
    st.remaining_before = Decimal("10.00")
    with pytest.raises(EngineInvariantError, match="remaining sum insured"):
        s12_invariants(st, i)
    # two lines: totals stay inside [0, claimed] but one line is individually broken
    two = CalcInput.model_validate(make_input([line("L1", "medicine", "100.00"), line("L2", "medicine", "100.00")]))
    st = EngineState.from_lines(two.lines)
    st.remaining_before = Decimal("1000.00")
    st.lines[0].ded = Decimal("110.00")  # net -10
    st.lines[1].allowed = Decimal("100.00")
    with pytest.raises(EngineInvariantError, match="negative payable"):
        s12_invariants(st, two)
    st = EngineState.from_lines(two.lines)
    st.remaining_before = Decimal("1000.00")
    st.lines[0].allowed = Decimal("-5.00")  # allowed out of range
    with pytest.raises(EngineInvariantError, match="allowed out of range"):
        s12_invariants(st, two)
    st, i = fresh()
    st.lines[0].ded = Decimal("0.001")
    with pytest.raises(EngineInvariantError, match="sub-cent"):
        s12_invariants(st, i)
    st, i = fresh()
    st.lines[0].allowed = Decimal("90.00")  # identity holds (10 disallowed) but no hit explains it
    with pytest.raises(EngineInvariantError, match="deductions"):
        s12_invariants(st, i)
    st, i = fresh()
    st.blocked = BlockedReason.POLICY_INACTIVE
    with pytest.raises(EngineInvariantError, match="blocked claim"):
        s12_invariants(st, i)


# ------------------------------------------------------------------ CLI
def test_cli_usage_explain_run_and_golden_update(capsys):
    assert cli.main([]) == 2 and "CLI" in capsys.readouterr().out
    assert cli.main(["bogus", "x"]) == 2
    g = next(GOLDEN.glob("ex*.json"))
    assert cli.main(["explain", str(g)]) == 0 and "payable" in capsys.readouterr().out.lower()
    assert cli.main(["run", str(g)]) == 0 and json.loads(capsys.readouterr().out)["payable_total"] is not None
    assert cli.main(["golden-update", str(GOLDEN)]) == 0 and "payable=" in capsys.readouterr().out


# ------------------------------------------------------------------ branch partials (the "not taken" side of each guard)
def go(lines, **kw):
    from calc_engine.engine import run

    return run(CalcInput.model_validate(make_input(lines, **kw)))


def test_declared_total_matching_raises_no_flag():
    r = go([line("L1", "medicine", "100.00")], declared_total="100.00")
    assert not any(f.code.value == "TOTAL_MISMATCH" for f in r.flags)


def test_pre_existing_prefix_that_does_not_match_the_diagnosis_is_ignored():
    r = go([line("L1", "medicine", "100.00")], pre_existing=["E11"], dx=["K35.8"], cover_start="2026-08-01")
    assert r.blocked is None or r.blocked.value != "PRE_EXISTING_WAITING"


def test_room_cap_with_no_proportionate_targets():
    # only the room line is billed and it is over the cap: nothing else to reduce proportionately
    r = go([line("L1", "room_rent", "30000.00", qty="3", unit_price="10000.00", days=3)], si="100000.00")
    assert r.payable_total < Decimal("30000.00") and not any(h.rule_id == "R-PROP-01" for ln in r.lines for h in ln.rule_trace)


def test_line_cap_not_exceeded_leaves_lines_untouched():
    r = go([line("L1", "ambulance", "1000.00")], rules_ov={"line_caps": {"ambulance": "3000"}})
    assert r.payable_total == Decimal("1000.00")


def test_copay_that_rounds_to_zero_changes_nothing():
    r = go([line("L1", "medicine", "0.04")], rules_ov={"co_pay": {"percent": "10", "conditions": {}}})
    assert r.payable_total == Decimal("0.04")


def test_sum_insured_cut_skips_lines_with_nothing_left_to_cut():
    # a zero-payable line (excluded) sits next to a payable one while the sum insured only covers part of the rest
    r = go([line("L1", "medicine", "60000.00"), line("L2", "other", "500.00", is_non_medical=True, exclusion_tags=["non_medical"])], si="50000.00")
    assert r.payable_total == Decimal("50000.00")


# ------------------------------------------------------------------ API token + error mapping
def test_require_service_auth_off_and_missing_secret(monkeypatch):
    from calc_engine import api
    from claim_contract.errors import ProblemError

    monkeypatch.setenv("CALC_ENGINE_AUTH", "off")
    assert api.require_service(None) == "dev"
    monkeypatch.setenv("CALC_ENGINE_AUTH", "on")
    monkeypatch.delenv("CALC_ENGINE_JWT_SECRET", raising=False)
    with pytest.raises(ProblemError) as e:
        api.require_service("Bearer x")
    assert e.value.status == 401


def test_run_maps_engine_errors_to_problem_codes(monkeypatch):
    from calc_engine import api
    from calc_engine.errors import RulesInvalidError, TooManyLinesError
    from claim_contract.errors import ProblemError

    for exc, code, status in ((TooManyLinesError("too many"), "payload_too_large", 413), (RulesInvalidError("bad rules"), "rules_invalid", 422), (EngineInvariantError("broken", []), "engine_invariant_violated", 500)):
        def boom(_inp, exc=exc):
            raise exc

        monkeypatch.setattr(api, "run", boom)
        with pytest.raises(ProblemError) as e:
            api._run(inp())
        assert (e.value.code, e.value.status) == (code, status)
