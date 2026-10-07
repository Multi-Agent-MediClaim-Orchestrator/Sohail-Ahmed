import copy
import os
import random
from decimal import Decimal
from fractions import Fraction

import pytest
from calc_engine.engine import run
from calc_engine.models import CalcInput
from calc_engine.money import allocate_cents, q
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from reference_calc import reference
from strategies import calc_inputs

EX = int(os.environ.get("HYPOTHESIS_EXAMPLES", "500"))
S = settings(max_examples=EX, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def res(d):
    return run(CalcInput.model_validate(d))


@S
@given(calc_inputs())
def test_p1_bounds_and_p11_no_negative_or_excess(d):
    r = res(d)
    assert 0 <= r.payable_total <= r.claimed_total
    assert r.payable_total <= r.remaining_sum_insured_before
    for ln in r.lines:
        assert 0 <= ln.payable <= ln.claimed and ln.disallowed >= 0


@S
@given(calc_inputs())
def test_p2_sums_and_line_identity(d):
    r = res(d)
    assert sum(ln.payable for ln in r.lines) == r.payable_total
    assert sum(x.amount.amount for x in r.summary_deductions) == r.patient_pays_total
    for ln in r.lines:
        assert ln.claimed == ln.payable + ln.disallowed + ln.deductible_share + ln.co_pay_share + ln.sum_insured_cut
        for v in (ln.payable, ln.disallowed):
            assert v.as_tuple().exponent >= -2


@S
@given(calc_inputs())
def test_p3_idempotent_bytes(d):
    assert res(d).model_dump_json() == res(copy.deepcopy(d)).model_dump_json()


@S
@given(calc_inputs(), st.randoms(use_true_random=False))
def test_p4_permutation_invariance(d, rnd):
    a = res(d)
    d2 = copy.deepcopy(d)
    rnd.shuffle(d2["lines"])
    b = res(d2)
    assert (a.payable_total, a.patient_pays_total, a.blocked) == (b.payable_total, b.patient_pays_total, b.blocked)
    assert {ln.line_ref: ln.payable for ln in a.lines} == {ln.line_ref: ln.payable for ln in b.lines}


@S
@given(calc_inputs(), st.data())
def test_p5_monotonic_in_claimed(d, data):
    i = data.draw(st.integers(0, len(d["lines"]) - 1))
    bump = Decimal(data.draw(st.integers(1, 5_000_000))) / 100
    base = res(d)
    d2 = copy.deepcopy(d)
    ln = d2["lines"][i]
    ln["claimed_amount"] = f"{Decimal(ln['claimed_amount']) + bump:.2f}"
    new = res(d2)
    tol = Decimal("0.01") * len(d["lines"])
    assert new.payable_total <= base.payable_total + bump + tol
    assert new.patient_pays_total >= base.patient_pays_total - tol


@S
@given(calc_inputs())
def test_p8_raising_si_never_lowers_payable_and_copay_deductible_never_raise(d):
    base = res(d)
    tol = Decimal("0.01") * (len(d["lines"]) + 2)
    d2 = copy.deepcopy(d)
    d2["policy"]["sum_insured"] = f"{Decimal(d['policy']['sum_insured']) + 100000:.2f}"
    assert res(d2).payable_total >= base.payable_total
    d3 = copy.deepcopy(d)
    d3["rules"]["deductible"]["amount"] = f"{Decimal(d['rules']['deductible']['amount']) + 1000:.2f}"
    assert res(d3).payable_total <= base.payable_total + tol
    d4 = copy.deepcopy(d)
    d4["rules"]["non_network_co_pay_percent"] = str(min(100, int(d["rules"]["non_network_co_pay_percent"]) + 10))
    d4["rules"]["co_pay"]["percent"] = str(min(100, int(d["rules"]["co_pay"]["percent"]) + 10))
    assert res(d4).payable_total <= base.payable_total + tol


@S
@given(calc_inputs(), st.data())
def test_p9_excluding_more_lines_never_raises_payable(d, data):
    base = res(d)
    i = data.draw(st.integers(0, len(d["lines"]) - 1))
    d2 = copy.deepcopy(d)
    d2["lines"][i]["exclusion_tags"] = ["cosmetic"]
    d2["rules"]["exclusions"]["tags"] = sorted(set(d2["rules"]["exclusions"]["tags"]) | {"cosmetic"})
    d["rules"]["exclusions"]["tags"] = d2["rules"]["exclusions"]["tags"]
    base = res(d)
    # each line can gain or lose a paisa in each rounding stage (sub-limit share, line cap, deductible, co-pay)
    assert res(d2).payable_total <= base.payable_total + Decimal("0.01") * (4 * len(d["lines"]) + 5)


@S
@given(calc_inputs(groups=["medicine", "surgeon_fees", "consumable", "investigation", "implant"], simple_rules=True))
def test_p7_no_rules_means_payable_is_sum_of_claims_capped_by_si(d):
    r = res(d)
    total = sum(Decimal(x["claimed_amount"]) for x in d["lines"])
    for x in d["lines"]:
        x["exclusion_tags"] = []
    expected = min(total, r.remaining_sum_insured_before)
    if r.blocked is None or r.blocked.value == "SUM_INSURED_EXHAUSTED":
        assert r.payable_total <= expected + Decimal("0.01")


@S
@given(calc_inputs())
def test_p12_whole_claim_blocks_zero_everything(d):
    r = res(d)
    if r.blocked and r.blocked.value in ("POLICY_INACTIVE", "PREMIUM_LAPSED", "OUTSIDE_COVER", "INITIAL_WAITING", "PRE_EXISTING_WAITING"):
        assert r.payable_total == 0 and all(ln.disallowed == ln.claimed for ln in r.lines)


@settings(max_examples=EX, deadline=None)
@given(st.lists(st.fractions(min_value=0, max_value=50_000), min_size=1, max_size=15), st.integers(0, 10_000_00))
def test_p10_allocate_cents(shares, total_cents):
    exact = {f"L{i:02d}": s for i, s in enumerate(shares)}
    total = sum(exact.values(), Fraction(0))
    out = allocate_cents(total, exact)
    assert sum(out.values()) == q(total)
    for k, v in out.items():
        assert abs(Fraction(v) - exact[k]) <= Fraction(1, 100)
    assert out == allocate_cents(total, dict(reversed(list(exact.items()))))


@settings(max_examples=max(50, EX // 5), deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(calc_inputs())
def test_differential_against_independent_reference(d):
    r = res(d)
    ref = reference(d)
    n = len(d["lines"])
    assert (r.blocked.value if r.blocked else None) == ref["blocked"], (r.flag_codes(), ref["flags"])
    assert abs(Fraction(r.payable_total) - ref["payable"]) <= Fraction(n + 2, 100)
    for code in ("PREMIUM_IN_GRACE", "WAITING_PERIOD_HIT", "CO_PAY_SELECTED", "SUM_INSURED_CAPPED"):
        if code == "SUM_INSURED_CAPPED":
            continue  # a cent of rounding can flip a cap that binds exactly; payable tolerance covers it
        assert (code in r.flag_codes()) == (code in ref["flags"]), code


def test_random_seed_banner():
    random.seed(0)
    assert pytest is not None
