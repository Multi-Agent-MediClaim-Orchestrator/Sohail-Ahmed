from decimal import Decimal as D

import pytest
from app.services import approval_rules as ar
from app.services.approval_rules import ApprovalError, Vote
from app.services.flags import ALL_FLAGS, FlagInputs, collect_review_flags
from app.services.gate import AutoFacts, Thresholds, auto_eligible, compute_gate, explain_gate
from app.verification import engine
from hypothesis import given
from hypothesis import strategies as st

TH = Thresholds(D("50000"), D("500000"), allow_auto=False)  # classic doc matrix: auto tier off, reviewer tier on
TH_AUTO = Thresholds(D("50000"), D("500000"), allow_auto=True)
CLEAN = AutoFacts("approve", 0, 0, 0.97, False, False)

# (id, claimed, payable, outcome, flags, allow_reviewer_final, expected tier) — 03-04 §9.1 G1..G10
MATRIX = [
    ("G1", "49999.99", "49999.99", "approve", [], True, "reviewer"),
    ("G2", "50000.00", "50000.00", "approve", [], True, "reviewer"),
    ("G3", "50000.01", "50000.01", "approve", [], True, "single_approver"),
    ("G4", "10000", "10000", "approve", ["fraud_warning"], True, "single_approver"),
    ("G5", "499999.99", "400000", "partial", [], True, "single_approver"),
    ("G6", "500000.00", "500000", "approve", [], True, "single_approver"),
    ("G7", "500000.01", "500000.01", "approve", [], True, "dual_approver"),
    ("G8", "600000", "0", "reject", [], True, "dual_approver"),
    ("G9", "80000", "20000", "partial", [], True, "single_approver"),
    ("G10", "20000", "20000", "approve", [], False, "single_approver"),
]


@pytest.mark.parametrize("id_,claimed,payable,outcome,flags,rev_final,tier", MATRIX, ids=[m[0] for m in MATRIX])
def test_gate_matrix(id_, claimed, payable, outcome, flags, rev_final, tier):
    th = Thresholds(D("50000"), D("500000"), allow_auto=False, allow_reviewer_final=rev_final)
    assert compute_gate(D(claimed), D(payable), outcome, th, flags).tier == tier


def test_dual_tier_requires_two_with_one_senior():
    g = compute_gate(D("600000"), D("600000"), "approve", TH, [])
    assert (g.required, g.min_senior, g.roles) == (2, 1, ("approver", "senior_reviewer"))


def test_auto_tier_only_for_clean_claims_within_t_auto():
    assert compute_gate(D("42000"), D("38500"), "approve", TH_AUTO, [], CLEAN).tier == "auto"
    assert compute_gate(D("50000"), D("50000"), "approve", TH_AUTO, [], CLEAN).tier == "auto"  # equality stays low
    assert compute_gate(D("50000.01"), D("40000"), "partial", TH_AUTO, [], CLEAN).tier == "single_approver"
    assert compute_gate(D("10000"), D("10000"), "approve", TH_AUTO, ["fraud_warning"], CLEAN).tier == "single_approver"  # a flag always needs a human
    assert compute_gate(D("30000"), D("30000"), "approve", TH_AUTO, [], AutoFacts("approve", 0, 1, 0.97, False, False)).tier == "reviewer"  # warning present
    assert compute_gate(D("30000"), D("30000"), "approve", TH_AUTO, [], AutoFacts("approve", 0, 0, 0.85, False, False)).tier == "reviewer"  # identity below 0.90
    assert compute_gate(D("30000"), D("0"), "reject", TH_AUTO, [], AutoFacts("reject", 0, 0, 0.97, False, False)).tier == "reviewer"  # rejections are never automatic
    assert compute_gate(D("30000"), D("30000"), "approve", TH_AUTO, [], AutoFacts("approve", 0, 0, 0.97, True, False)).tier == "reviewer"  # manual verification
    assert compute_gate(D("30000"), D("30000"), "approve", Thresholds(D("50000"), D("500000"), allow_auto=False), [], CLEAN).tier == "reviewer"
    assert compute_gate(D("600000"), D("600000"), "approve", TH_AUTO, [], CLEAN).tier == "dual_approver"


def test_auto_eligibility_reasons_are_explained():
    ok, why = auto_eligible(AutoFacts("approve", 1, 2, None, True, True), TH_AUTO, ["x"])
    assert not ok and {"blockers_present", "warnings_present", "identity_below_threshold", "degraded_or_manual_verification", "review_flags"} <= set(why)


def test_explain_text_comes_from_the_gate():
    g = compute_gate(D("620000"), D("620000"), "approve", TH, [])
    assert "Two approvers" in explain_gate(g, TH) and "T_four" in explain_gate(g, TH)


@given(st.integers(0, 2_000_000), st.integers(0, 2_000_000), st.integers(0, 2_000_000), st.sets(st.sampled_from(ALL_FLAGS)))
def test_gate_monotonic_in_amount_and_flags(a, b, extra, flagset):
    order = {"auto": 0, "reviewer": 1, "single_approver": 2, "dual_approver": 3}
    lo, hi = D(min(a, b)), D(max(a, b))
    g_lo = compute_gate(lo, lo, "approve", TH, sorted(flagset))
    g_hi = compute_gate(hi, hi, "approve", TH, sorted(flagset))
    assert order[g_hi.tier] >= order[g_lo.tier]
    more = sorted(flagset | {"fraud_warning"})
    assert order[compute_gate(lo, lo, "approve", TH, more).tier] >= order[g_lo.tier]


# ------------------------------------------------------------------ approval rules
def user_votes(*names, senior=()):
    return [Vote(n, "approve", frozenset({"senior_reviewer"} if n in senior else {"approver"})) for n in names]


def check(**kw):
    base = dict(user="a1", user_roles=frozenset({"approver"}), allowed_roles=frozenset({"approver", "senior_reviewer"}), verdict="approve", comment=None,
                prior_votes=[], submitter="r1", assignee="r1", sod=True)
    base.update(kw)
    ar.check_vote(**base)


def test_vote_rules():
    check()
    for kw, code in [({"user": "r1", "user_roles": frozenset({"approver"})}, "sod_violation"), ({"user_roles": frozenset({"reviewer"})}, "role_not_allowed"),
                     ({"prior_votes": user_votes("a1")}, "already_voted"), ({"verdict": "reject"}, "comment_required"), ({"verdict": "return", "comment": "  "}, "comment_required"),
                     ({"verdict": "maybe"}, "validation_error")]:
        with pytest.raises(ApprovalError) as ei:
            check(**kw)
        assert ei.value.code == code
    check(user="r1", sod=False)  # SoD can be disabled per environment
    check(verdict="return", comment="needs amount fix")


def test_finalize_rules_dual_needs_two_distinct_and_one_senior():
    assert ar.can_finalize(user_votes("a1"), 2, 1)[0] is False
    assert ar.can_finalize(user_votes("a1", "a2"), 2, 1) == (False, 0, 1)  # two non-seniors: stays open, senior needed
    assert ar.can_finalize(user_votes("a1", "s1", senior=("s1",)), 2, 1)[0] is True
    assert ar.can_finalize(user_votes("a1", "a1", senior=()), 2, 0)[0] is False  # same person twice is one vote
    assert ar.can_finalize(user_votes("a1"), 1, 0)[0] is True


@given(st.lists(st.sampled_from(["a1", "a2", "s1"]), max_size=12))
def test_no_sequence_of_votes_by_one_user_finalises_a_dual_task(seq):
    votes = [Vote(u, "approve", frozenset({"approver"})) for u in seq if u == "a1"]
    assert not ar.can_finalize(votes, 2, 1)[0]


# ------------------------------------------------------------------ flags
def fi(**kw):
    base = dict(findings=[], degraded=False, manual_verification=False, watchlist_hospital=False, utilised=D("0"), payable=D("1000"), sum_insured_total=D("500000"),
                approved_amount=D("1000"))
    base.update(kw)
    return FlagInputs(**base)


def mkf(code, sev, fixable=False, overridden=False):
    f = engine.make_finding(code, "x", severity=sev, fixable=fixable)
    f.overridden = overridden
    return f


@pytest.mark.parametrize("kw,flag", [
    ({"findings": [mkf("auth.tamper_suspected", "blocker", overridden=True)]}, "overridden_blocker"),
    ({"findings": [mkf("agent.disagrees_with_rules", "warning")]}, "agent_deterministic_disagreement"),
    ({"degraded": True}, "degraded_mode"), ({"manual_verification": True}, "degraded_mode"), ({"watchlist_hospital": True}, "watchlist_hospital"),
    ({"utilised": D("390000"), "approved_amount": D("20000")}, "high_utilisation"),
    ({"findings": [mkf("auth.fraud_warning", "warning")]}, "fraud_warning"), ({"approved_amount": D("1500")}, "manual_increase"), ({"escalated": True}, "escalated_case"),
])
def test_each_flag_is_raised_and_can_be_disabled(kw, flag):
    assert flag in collect_review_flags(fi(**kw), ALL_FLAGS)
    assert flag not in collect_review_flags(fi(**kw), [f for f in ALL_FLAGS if f != flag])


def test_no_flags_for_a_clean_claim_and_utilisation_boundary():
    assert collect_review_flags(fi(), ALL_FLAGS) == []
    assert "high_utilisation" not in collect_review_flags(fi(utilised=D("380000"), approved_amount=D("20000")), ALL_FLAGS)  # exactly 80%
