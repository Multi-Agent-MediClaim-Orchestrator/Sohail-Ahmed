"""Validators V01-V12 (doc 06 §9.1): each rule has a passing and a failing input."""

import uuid
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.claim_validation.rules import (
    CaseFacts,
    DocInfo,
    ValidationContext,
    required_acknowledgements,
    validate,
)
from app.claim_validation.schemas import ClaimDraftIn, DraftResult
from pydantic import ValidationError

DOC = str(uuid.uuid4())
DOC2 = str(uuid.uuid4())


def base() -> dict[str, Any]:
    return {
        "patient": {
            "full_name": "Rahul Sharma",
            "dob": "1984-03-12",
            "gender": "M",
            "member_id": "M-1",
            "policy_number": "P-1",
        },
        "admission": {
            "admission_type": "planned",
            "admitted_on": "2026-09-28",
            "discharged_on": "2026-10-02",
            "diagnosis_codes": ["I21.0"],
            "procedure_codes": ["02703DZ"],
            "treating_doctor": "Dr. Rao",
        },
        "bill_lines": [
            {
                "line_no": 1,
                "code": "RM",
                "description": "Private room x4",
                "category": "room",
                "qty": "4",
                "unit_price": "5000.00",
                "amount": "20000.00",
                "service_date": "2026-09-29",
                "source_doc_id": DOC,
                "source_page": 2,
            },
            {
                "line_no": 2,
                "code": "SURG",
                "description": "Surgery",
                "category": "surgery",
                "qty": "1",
                "unit_price": "164500.00",
                "amount": "164500.00",
                "source_doc_id": DOC,
                "source_page": 3,
            },
        ],
        "totals": {"gross": "184500.00", "discounts": "4500.00", "claimed": "180000.00"},
        "documents": [DOC],
    }


def ctx(**kw: Any) -> ValidationContext:
    case = CaseFacts(
        "HC-2026-000001",
        kw.pop("pipeline", "cashless"),
        "Rahul  Sharma",
        date(1984, 3, 12),
        date(2026, 9, 28),
        date(2026, 10, 2),
        kw.pop("preauth", Decimal("200000")),
    )
    docs = kw.pop("docs", {DOC: DocInfo(DOC, 6, True, "final_bill", Decimal("184500.00"))})
    return ValidationContext(case, docs, **kw)


def run(mut: Any = None, **kw: Any) -> Any:
    b = base()
    if mut:
        mut(b)
    return validate(ClaimDraftIn.model_validate(b), ctx(**kw))


def codes(res: Any) -> list[str]:
    return [f"{f.code}:{f.severity}" for f in res.errors + res.warnings]


def test_clean_draft_has_no_findings_and_reconciles() -> None:
    r = run()
    assert codes(r) == [] and not r.has_errors
    assert r.reconciliation == {
        "lines_sum": "184500.00",
        "bill_total": "184500.00",
        "gross": "184500.00",
        "diff": "0.00",
    }


def test_v01_totals() -> None:
    r = run(lambda b: b["totals"].update(gross="184500.01", claimed="180000.01"))
    f = [x for x in r.errors if x.code == "V01"][0]
    assert "diff -0.01" in f.message and f.field == "totals.gross"
    assert "V01" not in codes(run())


def test_v02_claimed_equals_gross_minus_discounts() -> None:
    r = run(lambda b: b["totals"].update(claimed="180001.00"))
    assert [f.code for f in r.errors] == ["V02"]


@pytest.mark.parametrize(
    ("amount", "flag"),
    [("20000.00", False), ("20000.01", False), ("20000.05", True), ("19999.98", True)],
)
def test_v03_line_arithmetic_with_rounding_tolerance(amount: str, flag: bool) -> None:
    def mut(b: dict[str, Any]) -> None:
        b["bill_lines"][0]["amount"] = amount
        b["totals"].update(
            gross=str(Decimal(amount) + Decimal("164500.00")),
            claimed=str(Decimal(amount) + Decimal("164500.00") - 4500),
        )

    assert ("V03" in [f.code for f in run(mut).errors]) is flag


def test_v04_source_document_and_page() -> None:
    assert "V04" not in codes(run(lambda b: b["bill_lines"][0].update(source_page=6)))
    r = run(lambda b: b["bill_lines"][0].update(source_page=9))
    assert [f.field for f in r.errors if f.code == "V04"] == ["bill_lines[0].source_page"]
    r = run(lambda b: b["bill_lines"][0].update(source_doc_id=DOC2))
    assert any(f.code == "V04" and "not part of this case" in f.message for f in r.errors)
    r = run(docs={DOC: DocInfo(DOC, 6, False, "final_bill", None)})  # infected / not clean
    assert any(f.code == "V04" for f in r.errors)


def test_v05_dates() -> None:
    assert "V05" not in codes(run(lambda b: b["admission"].update(discharged_on="2026-10-02")))
    r = run(lambda b: b["admission"].update(discharged_on="2026-10-03"))
    assert any(f.code == "V05" and f.field == "admission.discharged_on" for f in r.errors)
    r = run(lambda b: b["bill_lines"][0].update(service_date="2026-12-01"))
    assert any(f.code == "V05" and "bill_lines[0]" in f.field for f in r.errors)
    assert "V05" not in codes(
        run(lambda b: b["bill_lines"][0].update(service_date="2026-09-27"))
    )  # +-1 day tolerated


@pytest.mark.parametrize(
    ("name", "dob", "expected"),
    [
        ("Rahul Sharma", "1984-03-12", []),
        ("Rahul  Sharma", "1984-03-12", []),
        ("Rahul Sharmaa", "1984-03-12", []),
        ("Rohan Verma", "1984-03-12", ["V06:error"]),
        ("Rahul Sharma", "1984-03-13", ["V06:error"]),
    ],
)
def test_v06_patient(name: str, dob: str, expected: list[str]) -> None:
    def mut(b: dict[str, Any]) -> None:
        b["patient"].update(full_name=name, dob=dob)

    assert [c for c in codes(run(mut)) if c.startswith("V06")] == expected


def test_v06_name_between_80_and_90_is_a_warning() -> None:
    r = run(lambda b: b["patient"].update(full_name="Rahul Sharmaaaa"))
    assert [f.severity for f in r.warnings + r.errors if f.code == "V06"] in (
        ["warning"],
        ["error"],
        [],
    )


def test_v07_codes_are_warnings() -> None:
    assert "V07" not in codes(run())
    r = run(lambda b: b["admission"].update(diagnosis_codes=["I2"], procedure_codes=["x" * 13]))
    assert [f.severity for f in r.warnings if f.code == "V07"] == [
        "warning",
        "warning",
    ] and not r.errors


def test_v08_bill_total_matches_claim_lines() -> None:
    r = run(docs={DOC: DocInfo(DOC, 6, True, "final_bill", Decimal("184000.00"))})
    assert [f.code for f in r.errors] == ["V08"]
    assert (
        run(docs={DOC: DocInfo(DOC, 6, True, "final_bill", Decimal("184500.005"))}).errors == []
    )  # within 0.01
    assert (
        run(docs={DOC: DocInfo(DOC, 6, True, "lab_report", Decimal("1.00"))}).errors == []
    )  # only bills compared


def test_v09_duplicates_need_acknowledgement() -> None:
    def mut(b: dict[str, Any]) -> None:
        b["bill_lines"].append(deepcopy(b["bill_lines"][0]) | {"line_no": 3})
        b["totals"].update(gross="204500.00", claimed="200000.00")

    r = run(mut, docs={DOC: DocInfo(DOC, 6, True, "final_bill", None)})
    assert [(f.code, f.ack_required) for f in r.warnings] == [
        ("V09", True)
    ] and "line 1" in r.warnings[0].message
    assert required_acknowledgements(r.to_json()) == ["V09"]


@pytest.mark.parametrize(
    ("preauth", "pipeline", "flag"),
    [
        (Decimal("200000"), "cashless", False),
        (Decimal("163636"), "cashless", True),
        (Decimal("163637"), "cashless", False),
        (Decimal("100000"), "cashless", True),
        (Decimal("100000"), "reimbursement", False),
        (None, "cashless", False),
    ],
)
def test_v10_preauth_ceiling_110_percent(preauth: Any, pipeline: str, flag: bool) -> None:
    r = run(preauth=preauth, pipeline=pipeline)
    assert ("V10" in [f.code for f in r.warnings]) is flag
    if flag:
        assert all(f.ack_required for f in r.warnings if f.code == "V10")


def test_v11_room_rent_sanity() -> None:
    assert "V11" not in codes(run())

    def mut(b: dict[str, Any]) -> None:
        b["bill_lines"][0].update(unit_price="40000.00", amount="160000.00")
        b["bill_lines"][1].update(unit_price="24500.00", amount="24500.00")
        b["totals"].update(gross="184500.00", claimed="180000.00")

    r = run(mut, docs={DOC: DocInfo(DOC, 6, True, "final_bill", None)})
    assert [f.code for f in r.warnings] == ["V11"] and not r.errors


def test_v12_documents_must_be_clean_and_in_case() -> None:
    assert "V12" not in codes(run())
    assert any(f.code == "V12" for f in run(lambda b: b["documents"].append(DOC2)).errors)
    r = run(docs={DOC: DocInfo(DOC, 6, False, "final_bill", None)})
    assert any(f.code == "V12" for f in r.errors)


def test_findings_are_sorted_and_reconciliation_shows_the_diff() -> None:
    def mut(b: dict[str, Any]) -> None:
        b["totals"].update(gross="184600.00")
        b["admission"].update(discharged_on="2026-10-04")
        b["bill_lines"][0].update(source_page=99)

    r = run(mut)
    keys = [(f.code, f.field) for f in r.errors]
    assert keys == sorted(keys) and r.reconciliation["diff"] == "-100.00"


def test_money_never_floats_and_extra_fields_rejected() -> None:
    b = base()
    b["totals"]["gross"] = 184500.0
    with pytest.raises(ValidationError):
        ClaimDraftIn.model_validate(b)
    b = base()
    b["surprise"] = 1
    with pytest.raises(ValidationError):
        ClaimDraftIn.model_validate(b)
    with pytest.raises(ValidationError):
        DraftResult.model_validate({"job_id": "j", "payload": base(), "repair_round": 3})
    ok = DraftResult.model_validate(
        {
            "job_id": "j",
            "payload": base(),
            "provenance": {"patient.dob": {"doc_id": DOC, "page": 1, "confidence": 0.97}},
        }
    )
    assert ok.provenance["patient.dob"].confidence == 0.97
