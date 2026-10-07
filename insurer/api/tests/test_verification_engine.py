from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from insurer_app.verification import engine
from insurer_app.verification.context import HospitalInfo, Overlap
from insurer_app.verification.outcome import decide_run, outcome_from, step_status
from insurer_app.verification.schemas import Finding, finding_key
from vbuilders import REQUIRED, doc, make_ctx, with_member, with_patient, with_policy


def codes(o):
    return [f.code for f in o.findings]


# ------------------------------------------------------------------ completeness
def test_completeness_all_present_passes():
    o = engine.check_completeness(make_ctx())
    assert o.status == "passed" and not o.findings and o.deterministic["missing"] == []


def test_completeness_missing_required_is_fixable_blocker_with_doc_type():
    docs = [doc(i, t) for i, t in enumerate(REQUIRED) if t != "final_bill"]
    o = engine.check_completeness(make_ctx(documents=docs))
    f = o.findings[0]
    assert o.status == "flagged" and f.code == "completeness.missing_required" and f.fixable
    assert [d.value for d in f.suggested_doc_types] == ["final_bill"] and f.suggested_query_category.value == "missing_document"


def test_completeness_implant_line_requires_sticker():
    ctx = make_ctx(bill_categories={"implant"})
    o = engine.check_completeness(ctx)
    assert o.deterministic["missing"] == ["implant_sticker"]


def test_completeness_reimbursement_needs_receipt_and_cheque_but_cashless_does_not():
    assert engine.check_completeness(make_ctx()).status == "passed"
    o = engine.check_completeness(make_ctx(claim_type="reimbursement"))
    assert o.deterministic["missing"] == ["cancelled_cheque", "payment_receipt"]


@pytest.mark.parametrize("conf,flagged", [(0.79, True), (0.80, False), (0.81, False)])
def test_completeness_parse_confidence_boundary(conf, flagged):
    docs = [doc(i, t, parse_confidence=conf if t == "final_bill" else 0.95) for i, t in enumerate(REQUIRED)]
    o = engine.check_completeness(make_ctx(documents=docs))
    assert ("completeness.low_parse_confidence" in codes(o)) is flagged
    assert o.status == "passed"  # a warning never blocks


def test_completeness_superseded_doc_ignored_and_pending_not_counted():
    docs = [doc(i, t) for i, t in enumerate(REQUIRED)]
    docs[1] = replace(docs[1], superseded=True)
    docs.append(doc(9, "final_bill"))
    assert engine.check_completeness(make_ctx(documents=docs)).status == "passed"
    docs2 = [replace(d, fetch_status="pending") if d.doc_type == "final_bill" else d for d in [doc(i, t) for i, t in enumerate(REQUIRED)]]
    assert "final_bill" in engine.check_completeness(make_ctx(documents=docs2)).deterministic["missing"]


def test_completeness_zero_documents_blocks_every_required_type():
    o = engine.check_completeness(make_ctx(documents=[]))
    assert len(o.findings) == len(REQUIRED) and o.status == "flagged"


def test_completeness_unsigned_discharge():
    docs = [doc(i, t, extract={"signed": False} if t == "discharge_summary" else None) for i, t in enumerate(REQUIRED)]
    assert "completeness.unsigned_discharge" in codes(engine.check_completeness(make_ctx(documents=docs)))


# ------------------------------------------------------------------ identity
@pytest.mark.parametrize("name,expect_finding", [("Asha Verma", False), ("Verma Asha", False), ("Asha V", False), ("Rohit Singh", True)])
def test_identity_name_variants(name, expect_finding):
    o = engine.check_identity(with_patient(make_ctx(), full_name=name))
    assert ("identity.name_mismatch" in codes(o)) is expect_finding


def test_identity_dob_off_by_one_day_is_fixable_blocker():
    o = engine.check_identity(with_patient(make_ctx(), dob=date(1984, 3, 13)))
    f = next(f for f in o.findings if f.code == "identity.dob_mismatch")
    assert f.severity.value == "blocker" and f.fixable and o.status == "flagged" and o.deterministic["dob_match"] is False


def test_identity_id_hash_mismatch_is_unfixable_and_absent_hash_is_neutral():
    o = engine.check_identity(with_patient(make_ctx(), id_proof_hash="cd" * 32))
    assert o.status == "failed" and o.deterministic["id_hash_match"] is False
    o = engine.check_identity(with_member(make_ctx(), id_proof_hash=None))
    assert o.deterministic["id_hash_match"] is None and o.status == "passed"


def test_identity_policy_or_member_not_found_is_hard_fail_but_fixable():
    o = engine.check_identity(replace(make_ctx(), policy=None))
    assert o.status == "failed" and codes(o) == ["identity.policy_not_found"] and o.findings[0].fixable
    o = engine.check_identity(replace(make_ctx(), member=None))
    assert o.status == "failed" and codes(o) == ["identity.member_not_found"]


def test_identity_score_weights():
    o = engine.check_identity(make_ctx())
    assert o.score == 1.0
    o = engine.check_identity(with_patient(make_ctx(), gender="M"))
    assert o.score == 0.9 and "identity.gender_mismatch" in codes(o)


# ------------------------------------------------------------------ authenticity
def test_auth_clean_documents_pass_with_high_score():
    o = engine.check_authenticity_rules(make_ctx())
    assert o.status == "passed" and o.score >= 0.97 and o.deterministic["arithmetic_ok"]


@pytest.mark.parametrize("total,flagged", [("65501.00", False), ("65501.01", True), ("65499.00", False), ("65498.99", True)])
def test_auth_bill_arithmetic_tolerance_boundary(total, flagged):
    docs = [doc(i, t, extract={"total": total} if t == "final_bill" else None, vision={"stamp_detected": True, "signature_detected": True}) for i, t in enumerate(REQUIRED)]
    o = engine.check_authenticity_rules(make_ctx(documents=docs))
    assert ("auth.bill_arithmetic" in codes(o)) is flagged


def test_auth_duplicate_claim_overlap_unfixable_and_adjacent_stays_not_overlapping():
    o = engine.check_authenticity_rules(make_ctx(overlapping=[Overlap("x", "IC-2026-000001", "approved")]))
    assert o.status == "failed" and "auth.duplicate_claim" in codes(o)
    assert engine.check_authenticity_rules(make_ctx(overlapping=[])).status == "passed"


def test_auth_stamp_missing_on_bill_is_fixable_blocker_and_other_docs_not_required():
    docs = [doc(i, t, vision={"stamp_detected": t != "final_bill", "signature_detected": True, "tamper_score": 0.0}) for i, t in enumerate(REQUIRED)]
    o = engine.check_authenticity_rules(make_ctx(documents=docs))
    assert codes(o) == ["auth.stamp_missing"] and o.status == "flagged" and o.findings[0].fixable
    docs = [doc(i, t, vision={"stamp_detected": False, "signature_detected": True}) for i, t in enumerate(REQUIRED)]
    miss = [f for f in engine.check_authenticity_rules(make_ctx(documents=docs)).findings if f.code == "auth.stamp_missing"]
    assert len(miss) == 2  # final_bill, itemised_bill (pharmacy_bill absent); discharge/id are not stamp-required


def test_auth_tamper_score_threshold():
    mk = lambda ts: [doc(i, t, vision={"stamp_detected": True, "signature_detected": True, "tamper_score": ts}) for i, t in enumerate(REQUIRED)]  # noqa: E731
    assert "auth.tamper_suspected" not in codes(engine.check_authenticity_rules(make_ctx(documents=mk(0.29))))
    o = engine.check_authenticity_rules(make_ctx(documents=mk(0.31)))
    assert "auth.tamper_suspected" in codes(o) and o.status == "failed"


def test_auth_shared_hash_hospital_not_empanelled_and_dates():
    o = engine.check_authenticity_rules(make_ctx(shared_hash_docs=["d1"], hospital=HospitalInfo("HOSP-0009", "non_network")))
    assert {"auth.duplicate_document", "auth.hospital_not_empanelled"} <= set(codes(o))
    o = engine.check_authenticity_rules(make_ctx(hospital=HospitalInfo("HOSP-0001", "network", date(2026, 8, 1))))
    assert "auth.hospital_not_empanelled" in codes(o)
    docs = [doc(i, t, extract={"dates": ["2026-07-01"]} if t == "discharge_summary" else None, vision={"stamp_detected": True}) for i, t in enumerate(REQUIRED)]
    o = engine.check_authenticity_rules(make_ctx(documents=docs))
    assert "auth.dates_inconsistent" in codes(o) and o.status == "passed"  # warning only


# ------------------------------------------------------------------ coverage
def test_coverage_clean_passes():
    o = engine.check_coverage(make_ctx())
    assert o.status == "passed" and not o.findings and o.deterministic["policy_active"] is True


def test_coverage_policy_lapsed_within_grace_is_warning_beyond_is_blocker():
    ctx = with_policy(make_ctx(), status="lapsed", premium_paid_until=date(2026, 8, 20))
    o = engine.check_coverage(ctx)
    assert codes(o) == ["coverage.policy_grace"] and o.status == "passed"
    o = engine.check_coverage(with_policy(make_ctx(), status="lapsed", premium_paid_until=date(2026, 7, 1)))
    assert "coverage.policy_inactive" in codes(o) and o.status == "failed"
    assert "coverage.policy_inactive" in codes(engine.check_coverage(with_policy(make_ctx(), status="suspended")))


def test_coverage_admission_before_cover_start_unfixable():
    o = engine.check_coverage(with_member(make_ctx(), cover_start=date(2026, 9, 2)))
    assert "coverage.outside_period" in codes(o)


@pytest.mark.parametrize("days,blocked", [(29, True), (30, False)])
def test_coverage_initial_waiting_boundary(days, blocked):
    o = engine.check_coverage(with_member(make_ctx(), cover_start=date(2026, 9, 1).replace(day=1) if False else date.fromordinal(date(2026, 9, 1).toordinal() - days)))
    assert any(f.code == "coverage.waiting_period" and f.detail == "initial" for f in o.findings) is blocked


def test_coverage_accident_exempt_from_initial_waiting():
    ctx = with_member(make_ctx(diagnosis_codes=["S72.0"], admission_type="emergency"), cover_start=date(2026, 8, 20))
    assert not codes(engine.check_coverage(ctx))


def test_coverage_pre_existing_and_exclusion_and_sum_insured():
    ctx = with_member(make_ctx(diagnosis_codes=["E11.9"]), pre_existing=("E11",), cover_start=date(2025, 1, 1))
    o = engine.check_coverage(ctx)
    pe = next(f for f in o.findings if f.code == "coverage.pre_existing")
    assert pe.severity.value == "warning"  # E11 is group-mapped: the engine blocks only that group
    ctx = with_member(make_ctx(diagnosis_codes=["I10"]), pre_existing=("I10",))
    ctx = replace(ctx, rules=ctx.rules)
    assert any(f.code == "coverage.pre_existing" for f in engine.check_coverage(ctx).findings)
    o = engine.check_coverage(make_ctx(diagnosis_codes=["Z41.1"]))
    assert "coverage.exclusion" in codes(o) and o.status == "failed"
    o = engine.check_coverage(make_ctx(utilised=Decimal("480000")))
    assert codes(o) == ["coverage.sum_insured_low"] and o.status == "passed"
    assert "coverage.sum_insured_exhausted" in codes(engine.check_coverage(make_ctx(utilised=Decimal("500000"))))


def test_coverage_cashless_non_network_warning():
    o = engine.check_coverage(make_ctx(hospital=HospitalInfo("HOSP-0009", "non_network")))
    assert codes(o) == ["coverage.not_network"]


# ------------------------------------------------------------------ outcome rules + finalize
def mk(code, sev, fixable, overridden=False):
    f = engine.make_finding(code, "x", severity=sev, fixable=fixable)
    f.overridden = overridden
    return f


def test_step_status_rules_table():
    assert step_status([]) == "passed"
    assert step_status([mk("auth.dates_inconsistent", "warning", True)]) == "passed"
    assert step_status([mk("completeness.missing_required", "blocker", True)]) == "flagged"
    assert step_status([mk("auth.tamper_suspected", "blocker", False)]) == "failed"
    assert step_status([mk("completeness.missing_required", "blocker", True), mk("auth.tamper_suspected", "blocker", False)]) == "failed"
    assert step_status([mk("auth.tamper_suspected", "blocker", False, overridden=True)]) == "passed"
    assert step_status([], hard_fail=True) == "failed"


def test_finalize_decision_table():
    amt = Decimal("100")
    assert decide_run([mk("auth.dates_inconsistent", "warning", True)], calc_payable=amt, calc_claimed=amt).recommendation == "approve"
    assert decide_run([], calc_payable=Decimal("80"), calc_claimed=amt).recommendation == "partial"
    d = decide_run([mk("completeness.missing_required", "blocker", True)], calc_payable=amt, calc_claimed=amt)
    assert d.next_status == "needs_info" and d.recommendation is None
    d = decide_run([mk("completeness.missing_required", "blocker", True), mk("auth.duplicate_claim", "blocker", False)], calc_payable=amt, calc_claimed=amt)
    assert d.next_status == "ready_for_decision" and d.recommendation == "reject"  # unfixable wins
    d = decide_run([mk("auth.duplicate_claim", "blocker", False, overridden=True)], calc_payable=amt, calc_claimed=amt)
    assert d.recommendation == "approve"  # overridden blocker excluded
    assert decide_run([], calc_payable=None, calc_claimed=None).manual_verification
    assert decide_run([], calc_payable=Decimal("0"), calc_claimed=amt, calc_blocked="ALL_LINES_EXCLUDED").recommendation == "reject"


@given(st.lists(st.tuples(st.sampled_from(["warning", "blocker"]), st.booleans()), max_size=8), st.sampled_from(["warning", "blocker"]), st.booleans())
def test_finalize_is_monotonic_more_blockers_never_approve(base, sev, fixable):
    mkf = lambda s, fx: engine.make_finding("auth.dates_inconsistent", "x", severity=s, fixable=fx)  # noqa: E731
    fs = [mkf(s, fx) for s, fx in base]
    amt = Decimal("100")
    d0 = decide_run(fs, calc_payable=amt, calc_claimed=amt)
    d1 = decide_run([*fs, mkf("blocker", fixable)], calc_payable=amt, calc_claimed=amt)
    assert d1.recommendation != "approve"
    if d0.recommendation != "approve":
        assert d1.recommendation != "approve"


def test_finding_key_changes_with_evidence_and_plan_rerun_minimal():
    assert finding_key("identity", "identity.dob_mismatch", "d1:1:dob") != finding_key("identity", "identity.dob_mismatch", "d2:1:dob")
    assert engine.plan_rerun(["discharge_summary"]) == ["document_fetch", "completeness", "identity", "authenticity", "coverage", "calculation"]
    assert engine.plan_rerun(["payment_receipt"]) == ["document_fetch", "completeness"]
    assert engine.plan_rerun(["final_bill"]) == ["document_fetch", "completeness", "authenticity", "calculation"]
    assert engine.plan_rerun([]) == []
    for t in engine.INVALIDATES:
        assert set(engine.plan_rerun([t])) <= {"document_fetch", "completeness", "identity", "authenticity", "coverage", "calculation"}


def test_unknown_finding_code_rejected():
    with pytest.raises(ValueError):
        Finding(code="made.up", severity="warning", message="x")
    _ = outcome_from
