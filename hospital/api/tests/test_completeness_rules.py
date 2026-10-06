"""Pure unit tests for `evaluate` (doc 04 §9): table-driven scenarios, properties, determinism."""

import json
import random
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from app.completeness.context import (
    AdmissionFacts,
    CaseContext,
    DocFacts,
    PatientFacts,
    WaiverFacts,
)
from app.completeness.procedures import derive_procedure_group, has_surgery
from app.completeness.rules import (
    cross_checks,
    evaluate,
    has_field,
    name_ratio,
    parse_amount,
    parse_date,
    pick_best,
)
from app.completeness.schemas import DocRequirementsConfig, semantic_errors, validate_payload
from hypothesis import given, settings
from hypothesis import strategies as st
from seed.config_payloads import DOC_REQUIREMENTS

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CFG = DocRequirementsConfig.model_validate(DOC_REQUIREMENTS)
_ids = iter(range(10_000))


def doc(doc_type: str, **kw: Any) -> DocFacts:
    typed = kw.pop(
        "typed",
        {
            "patient_name": "Ravi Kumar",
            "date": "2026-09-29",
            "total": "1000.00",
            "lines": [{"amount": "1000.00"}],
            "medicines": ["Paracetamol"],
        },
    )
    base: dict[str, Any] = dict(
        doc_id=f"d{next(_ids):05d}",
        doc_type=doc_type,
        usable_state="ok",
        uploaded_at=NOW - timedelta(hours=1),
        quality_score=0.9,
        has_required_stamp=True,
        stamp_confidence=0.95,
        parse_confidence=0.92,
        agreement_score=0.97,
        typed_json=typed,
    )
    base.update(kw)
    return DocFacts(**base)


def make_ctx(*docs: DocFacts, **kw: Any) -> CaseContext:
    by: dict[str, list[DocFacts]] = {}
    for d in docs:
        by.setdefault(d.doc_type, []).append(d)
    base: dict[str, Any] = dict(
        case_id="c1",
        claim_type="cashless",
        admission_type="planned",
        procedure_group=None,
        flags=frozenset(),
        patient=PatientFacts("Ravi Kumar", policy_number="AH-993201"),
        admission=AdmissionFacts(date(2026, 9, 28), date(2026, 10, 2), ("K80.2",), ()),
        docs_by_type=by,
        now=NOW,
    )
    base.update(kw)
    return CaseContext(**base)


def happy() -> list[DocFacts]:
    return [doc("prescription"), doc("pharmacy_bill"), doc("final_bill")]


def statuses(ctx: CaseContext, cfg: DocRequirementsConfig = CFG) -> dict[str, str]:
    return {i.rule_id: i.status for i in evaluate(ctx, cfg).items}


def item(ctx: CaseContext, rule_id: str, cfg: DocRequirementsConfig = CFG) -> Any:
    return next(i for i in evaluate(ctx, cfg).items if i.rule_id == rule_id)


# ---------------------------------------------------------------------------- T01-T20 (spec matrix)
def test_t01_happy_path() -> None:
    r = evaluate(make_ctx(*happy()), CFG)
    assert r.complete and not r.provisional
    assert {i.rule_id: i.status for i in r.items} == {
        "R-BILL-01": "present_ok",
        "R-PH-01": "present_ok",
        "R-RX-01": "present_ok",
    }


@pytest.mark.parametrize(
    ("missing", "rule"),
    [("prescription", "R-RX-01"), ("pharmacy_bill", "R-PH-01"), ("final_bill", "R-BILL-01")],
)
def test_t02_t03_missing_required(missing: str, rule: str) -> None:
    ctx = make_ctx(*[d for d in happy() if d.doc_type != missing])
    r = evaluate(ctx, CFG)
    it = next(i for i in r.items if i.rule_id == rule)
    assert (it.status, it.severity, it.reasons) == (
        "missing",
        "blocker",
        ["not_uploaded"],
    ) and not r.complete
    assert "required" in it.message


def test_t04_surgery_without_procedure_bill_and_with() -> None:
    ctx = make_ctx(*happy(), flags=frozenset({"has_surgery"}))
    assert item(ctx, "R-PROC-01").status == "missing"
    ctx2 = make_ctx(*happy(), doc("procedure_bill"), flags=frozenset({"has_surgery"}))
    assert item(ctx2, "R-PROC-01").status == "present_ok" and evaluate(ctx2, CFG).complete
    assert "R-PROC-01" not in statuses(
        make_ctx(*happy())
    )  # not applicable without the flag -> no item


def test_t05_blurry_bill() -> None:
    ds = [
        doc("prescription"),
        doc("pharmacy_bill"),
        doc("final_bill", quality_flags=frozenset({"blurry"})),
    ]
    it = item(make_ctx(*ds), "R-BILL-01")
    assert (it.status, it.severity, it.reasons[0]) == (
        "unusable",
        "blocker",
        "blurry",
    ) and "re-scan" in it.message


def test_t06_pharmacy_bill_without_stamp_is_fixable_not_fatal() -> None:
    ds = [doc("prescription"), doc("pharmacy_bill", has_required_stamp=False), doc("final_bill")]
    it = item(make_ctx(*ds), "R-PH-01")
    assert (it.status, it.reasons) == (
        "unusable",
        ["stamp_missing"],
    ) and "stamped copy" in it.message
    assert not evaluate(make_ctx(*ds), CFG).complete


def test_t07_low_parse_confidence() -> None:
    ds = [doc("prescription", parse_confidence=0.62), doc("pharmacy_bill"), doc("final_bill")]
    r = evaluate(make_ctx(*ds), CFG)
    it = next(i for i in r.items if i.rule_id == "R-RX-01")
    assert (it.status, it.reasons) == ("needs_review", ["low_parse_confidence"]) and not r.complete


def test_t08_pass_disagreement() -> None:
    ds = [doc("prescription", agreement_score=0.55), doc("pharmacy_bill"), doc("final_bill")]
    it = item(make_ctx(*ds), "R-RX-01")
    assert (it.status, it.reasons) == ("needs_review", ["passes_disagree"])


def test_t09_t10_knee_implant_sticker() -> None:
    groups = DOC_REQUIREMENTS["procedure_groups"]
    grp = derive_procedure_group(
        ["0SRD0J9"], {"ortho_implant": ["0SR*"], "cardiac_stent": groups["cardiac_stent"]}
    )
    assert grp == "ortho_implant"
    ctx = make_ctx(*happy(), procedure_group=grp)
    assert item(ctx, "R-IMP-02").status == "missing" and not evaluate(ctx, CFG).complete
    ctx2 = make_ctx(
        *happy(), doc("implant_sticker", has_required_stamp=None, typed={}), procedure_group=grp
    )
    assert item(ctx2, "R-IMP-02").status == "present_ok" and evaluate(ctx2, CFG).complete


def test_t11_alternative_itemised_instead_of_final_bill() -> None:
    ds = [doc("prescription"), doc("pharmacy_bill"), doc("itemised_bill")]
    it = item(make_ctx(*ds), "R-BILL-01")
    assert it.status == "present_ok" and it.via == "itemised_bill"
    cfg = deepcopy(DOC_REQUIREMENTS)
    cfg["alternatives"] = []
    assert (
        item(make_ctx(*ds), "R-BILL-01", DocRequirementsConfig.model_validate(cfg)).status
        == "missing"
    )


def test_t12_waived_counts_as_satisfied() -> None:
    ctx = make_ctx(
        *happy(),
        flags=frozenset({"has_surgery"}),
        waivers={"R-PROC-01": WaiverFacts("R-PROC-01", "included in the final bill")},
    )
    r = evaluate(ctx, CFG)
    assert next(i for i in r.items if i.rule_id == "R-PROC-01").status == "waived" and r.complete


def test_t13_bill_dated_before_prescription_is_a_warning_only() -> None:
    ds = [
        doc(
            "prescription",
            typed={"patient_name": "Ravi Kumar", "date": "2026-09-30", "medicines": ["x"]},
        ),
        doc(
            "pharmacy_bill",
            typed={"date": "2026-09-29", "total": "10", "lines": [{"amount": "10"}]},
        ),
        doc("final_bill"),
    ]
    r = evaluate(make_ctx(*ds), CFG)
    o = next(i for i in r.items if i.rule_id == "R-ORD-01")
    assert (o.status, o.severity, o.reasons) == (
        "needs_review",
        "warning",
        ["not_chronological"],
    ) and r.complete


def test_t14_two_bills_one_blurry_each_must_pass() -> None:
    ds = [
        doc("prescription"),
        doc("pharmacy_bill"),
        doc("final_bill"),
        doc("final_bill", quality_flags=frozenset({"blurry"})),
    ]
    it = item(make_ctx(*ds), "R-BILL-01")
    assert it.status == "unusable" and len(it.document_ids) == 2
    cfg = deepcopy(DOC_REQUIREMENTS)
    next(r for r in cfg["rules"] if r["id"] == "R-BILL-01")["any_candidate"] = True
    assert (
        item(make_ctx(*ds), "R-BILL-01", DocRequirementsConfig.model_validate(cfg)).status
        == "present_ok"
    )


def test_t15_pending_parse_is_provisional() -> None:
    ds = [
        doc("prescription", usable_state="pending_processing"),
        doc("pharmacy_bill"),
        doc("final_bill"),
    ]
    r = evaluate(make_ctx(*ds), CFG)
    assert next(i for i in r.items if i.rule_id == "R-RX-01").status == "pending_processing"
    assert r.provisional and not r.complete


def test_t16_name_mismatch_is_warning_only() -> None:
    ds = [
        doc(
            "prescription",
            typed={"patient_name": "Rakesh Sharma", "date": "2026-09-29", "medicines": ["x"]},
        ),
        doc("pharmacy_bill"),
        doc("final_bill"),
    ]
    r = evaluate(make_ctx(*ds, patient=PatientFacts("Rahul Sharma")), CFG)
    x = next(i for i in r.items if i.rule_id == "X-01")
    assert (x.severity, x.reasons) == ("warning", ["patient_name_mismatch"]) and r.complete


def test_t17_bill_total_mismatch_blocks() -> None:
    ds = [
        doc("prescription"),
        doc("pharmacy_bill"),
        doc(
            "final_bill",
            typed={
                "total": "1,80,000",
                "date": "2026-09-30",
                "lines": [{"amount": "100000"}, {"amount": "84500"}],
            },
        ),
    ]
    r = evaluate(make_ctx(*ds), CFG)
    x = next(i for i in r.items if i.rule_id == "X-04")
    assert (x.severity, x.reasons) == ("blocker", ["bill_total_mismatch"]) and not r.complete
    ok = doc(
        "final_bill",
        typed={
            "total": "2,00,000",
            "date": "2026-09-30",
            "lines": [{"amount": "100000"}, {"amount": "84500"}],
        },
    )
    assert "X-04" not in statuses(
        make_ctx(doc("prescription"), doc("pharmacy_bill"), ok)
    )  # taxes above lines are fine


def test_t18_infected_only_candidate_is_missing_with_specific_message() -> None:
    ds = [
        doc("prescription", usable_state="excluded", excluded_reason="infected"),
        doc("pharmacy_bill"),
        doc("final_bill"),
    ]
    it = item(make_ctx(*ds), "R-RX-01")
    assert it.status == "missing" and "virus scan" in it.message


def test_t19_superseded_ignored() -> None:
    old = doc(
        "final_bill",
        quality_flags=frozenset({"blurry"}),
        usable_state="excluded",
        excluded_reason="superseded",
    )
    r = evaluate(make_ctx(doc("prescription"), doc("pharmacy_bill"), old, doc("final_bill")), CFG)
    assert r.complete and next(i for i in r.items if i.rule_id == "R-BILL-01").document_ids != [
        old.doc_id
    ]


def test_t20_date_out_of_window() -> None:
    ds = [
        doc(
            "prescription",
            typed={"patient_name": "Ravi Kumar", "date": "2026-04-01", "medicines": ["x"]},
        ),
        doc("pharmacy_bill"),
        doc("final_bill"),
    ]
    r = evaluate(make_ctx(*ds), CFG)
    x = next(i for i in r.items if i.rule_id == "X-05")
    assert (x.severity, x.reasons) == ("warning", ["date_out_of_window"]) and r.complete
    ok = [
        doc(
            "prescription",
            typed={"patient_name": "Ravi Kumar", "date": "2026-09-10", "medicines": ["x"]},
        ),
        doc("pharmacy_bill"),
        doc("final_bill"),
    ]
    assert "X-05" not in statuses(
        make_ctx(*ok)
    )  # pre-admission prescription within 30 days is exempt


# ------------------------------------------------------------------------------------ more behaviour
def test_stamp_states() -> None:
    pending = doc("pharmacy_bill", has_required_stamp=None, uploaded_at=NOW - timedelta(minutes=2))
    assert (
        item(make_ctx(doc("prescription"), pending, doc("final_bill")), "R-PH-01").status
        == "pending_processing"
    )
    stale = doc("pharmacy_bill", has_required_stamp=None, uploaded_at=NOW - timedelta(minutes=30))
    it = item(make_ctx(doc("prescription"), stale, doc("final_bill")), "R-PH-01")
    assert (it.status, it.reasons) == (
        "needs_review",
        ["stamp_not_evaluated"],
    )  # vision-service timeout
    weak = doc("pharmacy_bill", stamp_confidence=0.4)
    it = item(make_ctx(doc("prescription"), weak, doc("final_bill")), "R-PH-01")
    assert (it.status, it.severity) == (
        "needs_review",
        "warning",
    )  # officer review, does not block alone
    assert evaluate(make_ctx(doc("prescription"), weak, doc("final_bill")), CFG).complete


def test_required_fields_and_aliases() -> None:
    no_total = doc("final_bill", typed={"date": "2026-09-30", "lines": [{"amount": "1"}]})
    it = item(make_ctx(doc("prescription"), doc("pharmacy_bill"), no_total), "R-BILL-01")
    assert (it.status, it.reasons) == ("unusable", ["field_missing:total"])
    aliased = doc(
        "final_bill",
        typed={"grand_total": "10", "line_items": [{"amount": "10"}], "date": "2026-09-30"},
    )
    assert (
        item(make_ctx(doc("prescription"), doc("pharmacy_bill"), aliased), "R-BILL-01").status
        == "present_ok"
    )
    assert has_field(doc("x", typed={"patient": {"name": "A"}}), "patient_name")
    assert not has_field(doc("x", typed=None), "total")


def test_low_resolution_is_a_warning() -> None:
    ds = [
        doc("prescription"),
        doc("pharmacy_bill", quality_flags=frozenset({"glare"})),
        doc("final_bill"),
    ]
    r = evaluate(make_ctx(*ds), CFG)
    it = next(i for i in r.items if i.rule_id == "R-PH-01")
    assert (it.status, it.severity) == ("present_ok", "warning") and r.complete


def test_unclassified_documents_block_until_confirmed() -> None:
    r = evaluate(make_ctx(*happy(), unclassified_ids=("u2", "u1")), CFG)
    c = next(i for i in r.items if i.rule_id == "R-CLS-01")
    assert c.document_ids == ["u1", "u2"] and not r.complete


def test_not_applicable_only_when_router_says_codes_pending() -> None:
    plain = evaluate(make_ctx(*happy()), CFG)
    assert (
        "R-IMP-02" not in {i.rule_id for i in plain.items} and not plain.provisional
    )  # plain medical admission
    r = evaluate(make_ctx(*happy(), flags=frozenset({"procedure_codes_pending"})), CFG)
    imp = next(i for i in r.items if i.rule_id == "R-IMP-02")
    assert (
        imp.status == "not_applicable" and r.provisional and r.complete
    )  # flagged, but does not block


def test_x02_dob_conflict_blocker_only_when_two_confident_docs_disagree() -> None:
    a = doc("discharge_summary", typed={"dob": "1984-03-12"}, parse_confidence=0.95)
    b = doc("lab_report", typed={"dob": "1990-01-01"}, parse_confidence=0.95)
    x = [i for i in cross_checks(make_ctx(a, b), CFG) if i.rule_id == "X-02"][0]
    assert x.severity == "blocker"
    b2 = replace(b, parse_confidence=0.6)
    assert [i for i in cross_checks(make_ctx(a, b2), CFG) if i.rule_id == "X-02"][
        0
    ].severity == "warning"


def test_x03_x06_x07() -> None:
    ds = doc(
        "discharge_summary", typed={"admitted_on": "2026-09-20", "discharged_on": "2026-10-02"}
    )
    ids = {i.rule_id for i in cross_checks(make_ctx(ds), CFG)}
    assert "X-03" in ids
    emergency = make_ctx(
        doc(
            "discharge_summary", typed={"admitted_on": "2026-09-29", "discharged_on": "2026-10-02"}
        ),
        admission_type="emergency",
    )
    assert "X-03" not in {i.rule_id for i in cross_checks(emergency, CFG)}  # ±1 day tolerated
    card = doc("policy_card", typed={"policy_number": "AH 993-201"})
    assert "X-06" not in {i.rule_id for i in cross_checks(make_ctx(card), CFG)}
    card2 = doc("policy_card", typed={"policy_number": "ZZ-1"})
    assert "X-06" in {i.rule_id for i in cross_checks(make_ctx(card2), CFG)}
    h = [
        doc("final_bill", typed={"hospital_name": "City Care Hospital"}),
        doc("pharmacy_bill", typed={"hospital_name": "Sunrise Clinic"}),
    ]
    assert "X-07" in {i.rule_id for i in cross_checks(make_ctx(*h), CFG)}


# ------------------------------------------------------------------------------------- properties
def result_json(ctx: CaseContext, cfg: DocRequirementsConfig = CFG) -> bytes:
    return json.dumps(evaluate(ctx, cfg).model_dump(mode="json"), sort_keys=True).encode()


def test_idempotent_and_order_independent() -> None:
    ds = happy() + [doc("final_bill", quality_flags=frozenset({"blurry"})), doc("lab_report")]
    ctx = make_ctx(*ds, flags=frozenset({"has_surgery"}))
    first = result_json(ctx)
    assert first == result_json(ctx)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = {k: rng.sample(v, len(v)) for k, v in ctx.docs_by_type.items()}
        cfg = deepcopy(DOC_REQUIREMENTS)
        rng.shuffle(cfg["rules"])
        assert (
            result_json(
                replace(ctx, docs_by_type=shuffled), DocRequirementsConfig.model_validate(cfg)
            )
            == first
        )


def score(r: Any) -> int:
    return -sum(
        1
        for i in r.items
        if i.severity in ("blocker", "review") or i.status == "pending_processing"
    )


@settings(max_examples=60, deadline=None)
@given(
    st.sets(st.sampled_from(["prescription", "pharmacy_bill", "final_bill"])),
    st.sampled_from(["prescription", "pharmacy_bill", "final_bill"]),
)
def test_adding_a_satisfying_document_never_worsens(have: set[str], add: str) -> None:
    before = make_ctx(*[doc(t) for t in sorted(have)])
    after = make_ctx(*[doc(t) for t in sorted(have | {add})])
    assert score(evaluate(after, CFG)) >= score(evaluate(before, CFG))


def test_seed_config_validates_and_semantic_errors() -> None:
    cfg, errs = validate_payload(DOC_REQUIREMENTS)
    assert cfg is not None and errs == []
    bad = deepcopy(DOC_REQUIREMENTS)
    bad["rules"].append(deepcopy(bad["rules"][0]))
    bad["alternatives"].append({"any_of": ["final_bill"], "for": "R-NOPE"})
    bad["rules"].append(
        {"id": "R-X", "doc_type": "final_bill", "applies": {"procedure_group": ["ghost"]}}
    )
    _, errs = validate_payload(bad)
    assert any("duplicate rule id R-RX-01" in e for e in errs) and any(
        "unknown rule" in e for e in errs
    )
    assert any("ghost" in e for e in errs)
    assert validate_payload({"rules": [{"id": "A", "doc_type": "not_a_type"}]})[0] is None
    assert validate_payload({"rules": []})[0] is None
    assert (
        validate_payload({"rules": [{"id": "A", "doc_type": "final_bill", "typo": 1}]})[0] is None
    )
    cfg2 = DocRequirementsConfig.model_validate(
        {"rules": [{"id": "A", "doc_type": "final_bill", "requirement": "conditional"}]}
    )
    assert any("no applicability" in e for e in semantic_errors(cfg2))


def test_helpers() -> None:
    assert parse_date("2026-09-29T10:00:00Z") == date(2026, 9, 29) and parse_date(
        "29/09/2026"
    ) == date(2026, 9, 29)
    assert parse_date("31/02/2026") is None and parse_date(None) is None and parse_date(5) is None
    assert (
        parse_amount("Rs. 1,48,230/-") == parse_amount("148230.00") and parse_amount("abc") is None
    )
    assert (
        name_ratio("Mr. Ravi Kumar", "Ravi  Kumar") == 100
        and name_ratio("Rahul Sharma", "Rakesh Sharma") < 90
    )
    assert has_surgery(["0FT44ZZ"]) and not has_surgery(["BW20ZZZ", ""])
    assert (
        derive_procedure_group(["02703ZZ"], DOC_REQUIREMENTS["procedure_groups"]) == "cardiac_stent"
    )
    assert derive_procedure_group(["XYZ"], DOC_REQUIREMENTS["procedure_groups"]) is None
    a, b = doc("final_bill", quality_score=0.5), doc("final_bill", quality_score=0.9)
    assert pick_best([a, b])[0] is b
    blocked = doc("final_bill", quality_score=0.99, quality_flags=frozenset({"blurry"}))
    assert pick_best([blocked, a])[0] is a  # fewer blocking flags beat higher quality


def test_evaluate_forty_documents_is_fast() -> None:
    import time

    ds = happy() + [doc("lab_report") for _ in range(37)]
    ctx = make_ctx(*ds)
    t = time.perf_counter()
    evaluate(ctx, CFG)
    assert time.perf_counter() - t < 0.1
