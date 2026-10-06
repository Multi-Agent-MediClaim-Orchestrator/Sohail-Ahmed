import copy
from typing import Any

import pytest
from claim_contract.errors import from_validation_error
from claim_contract.models import ClaimSubmission, Money, QueryResponse
from pydantic import ValidationError
from sample_claim import valid_submission

DOC2 = "0199a1b2-1111-7000-8000-0000000000ff"


def test_valid(submission: dict[str, Any]) -> None:
    c = ClaimSubmission.model_validate(submission)
    assert c.totals.claimed.amount == 12000
    dumped = c.model_dump(mode="json")
    assert dumped["totals"]["gross"]["amount"] == "12000.00"  # money serialises as string
    assert ClaimSubmission.model_validate_json(c.model_dump_json()) == c


def _mutate(fn: Any) -> dict[str, Any]:
    d = valid_submission()
    fn(d)
    return d


CASES = [
    (
        "totals_off_by_paisa",
        lambda d: d["totals"]["gross"].update(amount="12000.01"),
        "totals_mismatch",
    ),
    (
        "claimed_wrong",
        lambda d: d["totals"]["claimed"].update(amount="11000.00"),
        "totals_mismatch",
    ),
    (
        "discharge_before_admit",
        lambda d: d["admission"].update(discharged_on="2026-09-01"),
        "validation_error",
    ),
    (
        "unknown_source_doc",
        lambda d: d["bill_lines"][0].update(source_doc_id=DOC2),
        "validation_error",
    ),
    ("cashless_no_preauth", lambda d: d["admission"].update(preauth_ref=None), "validation_error"),
    (
        "service_date_outside",
        lambda d: d["bill_lines"][0].update(service_date="2026-12-01"),
        "validation_error",
    ),
    (
        "raw_aadhaar_in_notes",
        lambda d: d.update(hospital_notes="patient 1234 5678 9012"),
        "validation_error",
    ),
    ("float_money", lambda d: d["totals"]["gross"].update(amount=12000.0), "validation_error"),
    ("extra_field", lambda d: d.update(surprise=1), "validation_error"),
    ("malformed_icd", lambda d: d["admission"].update(diagnosis_codes=["xx"]), "validation_error"),
    (
        "negative_amount",
        lambda d: d["bill_lines"][0]["amount"].update(amount="-1"),
        "validation_error",
    ),
    (
        "line_amount_mismatch",
        lambda d: d["bill_lines"][0]["amount"].update(amount="11000.00"),
        "totals_mismatch",
    ),
    ("bad_claim_ref", lambda d: d.update(claim_ref="X-1"), "validation_error"),
]


@pytest.mark.parametrize(("name", "fn", "code"), CASES, ids=[c[0] for c in CASES])
def test_invalid(name: str, fn: Any, code: str) -> None:
    with pytest.raises(ValidationError) as ei:
        ClaimSubmission.model_validate(_mutate(fn))
    assert from_validation_error(ei.value).code == code


def test_duplicate_line_id(submission: dict[str, Any]) -> None:
    submission["bill_lines"].append(copy.deepcopy(submission["bill_lines"][0]))
    submission["totals"]["gross"]["amount"] = "24000.00"
    submission["totals"]["claimed"]["amount"] = "24000.00"
    with pytest.raises(ValidationError, match="duplicate line_id"):
        ClaimSubmission.model_validate(submission)


def test_emergency_cashless_without_preauth_ok(submission: dict[str, Any]) -> None:
    submission["admission"].update(admission_type="emergency", preauth_ref=None)
    ClaimSubmission.model_validate(submission)


def test_empty_query_response() -> None:
    with pytest.raises(ValidationError):
        QueryResponse(
            query_id="0199a1b2-7777-7000-8000-000000000031",  # type: ignore[arg-type]
            responded_by="o1",
            responded_at="2026-10-02T14:20:00Z",  # type: ignore[arg-type]
        )


def test_money_rounding_and_float() -> None:
    assert str(Money.of("1.005").amount) == "1.01"
    with pytest.raises(ValidationError):
        Money(amount=1.5)  # type: ignore[arg-type]
