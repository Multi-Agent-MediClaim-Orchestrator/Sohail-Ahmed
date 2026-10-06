"""Generate contract test fixtures (5 valid, 15 invalid) from the sample claim. Run: make fixtures"""

import copy
import json
import pathlib
import sys

sys.path.insert(0, "contract/tests")
from sample_claim import DOC, valid_submission  # noqa: E402

OUT = pathlib.Path("contract/tests/fixtures")
(OUT / "valid").mkdir(parents=True, exist_ok=True)
(OUT / "invalid").mkdir(parents=True, exist_ok=True)


def dump(path: pathlib.Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def variant(**changes: object) -> dict:
    d = copy.deepcopy(valid_submission())
    for k, v in changes.items():
        d[k] = v
    return d


def big(n: int) -> dict:
    d = valid_submission()
    line = d["bill_lines"][0]
    d["bill_lines"] = []
    for i in range(1, n + 1):
        ln = copy.deepcopy(line)
        ln["line_id"], ln["qty"] = f"L{i:04d}", "1"
        ln["unit_price"], ln["amount"] = {"amount": "100.00"}, {"amount": "100.00"}
        d["bill_lines"].append(ln)
    total = f"{n * 100}.00"
    d["totals"] = {
        "gross": {"amount": total},
        "discounts": {"amount": "0.00"},
        "claimed": {"amount": total},
    }
    return d


valid = {
    "cashless_planned_01": valid_submission(),
    "cashless_emergency_01": variant(),
    "reimbursement_planned_01": variant(claim_type="reimbursement"),
    "reimbursement_emergency_01": variant(claim_type="reimbursement"),
    "multi_document_large_01": big(1500),
}
valid["cashless_emergency_01"]["admission"].update(admission_type="emergency", preauth_ref=None)
valid["reimbursement_planned_01"]["admission"]["preauth_ref"] = None
valid["reimbursement_emergency_01"]["admission"].update(
    admission_type="emergency", preauth_ref=None
)
for n, d in valid.items():
    dump(OUT / "valid" / f"{n}.json", d)


def mut(fn):  # type: ignore[no-untyped-def]
    d = valid_submission()
    fn(d)
    return d


invalid = {
    "totals_off_by_paisa": (
        mut(lambda d: d["totals"]["gross"].update(amount="12000.01")),
        "totals_mismatch",
    ),
    "claimed_not_gross_minus_discounts": (
        mut(lambda d: d["totals"]["claimed"].update(amount="11000.00")),
        "totals_mismatch",
    ),
    "line_amount_mismatch": (
        mut(lambda d: d["bill_lines"][0]["amount"].update(amount="11000.00")),
        "totals_mismatch",
    ),
    "discharge_before_admit": (
        mut(lambda d: d["admission"].update(discharged_on="2026-09-01")),
        "validation_error",
    ),
    "unknown_source_doc": (
        mut(
            lambda d: d["bill_lines"][0].update(
                source_doc_id="0199a1b2-1111-7000-8000-0000000000ff"
            )
        ),
        "validation_error",
    ),
    "cashless_no_preauth": (
        mut(lambda d: d["admission"].update(preauth_ref=None)),
        "validation_error",
    ),
    "service_date_outside": (
        mut(lambda d: d["bill_lines"][0].update(service_date="2026-12-01")),
        "validation_error",
    ),
    "raw_aadhaar_in_notes": (
        mut(lambda d: d.update(hospital_notes="patient 1234 5678 9012")),
        "validation_error",
    ),
    "float_money": (mut(lambda d: d["totals"]["gross"].update(amount=12000.0)), "validation_error"),
    "extra_field": (mut(lambda d: d.update(surprise=1)), "validation_error"),
    "malformed_icd": (
        mut(lambda d: d["admission"].update(diagnosis_codes=["xx"])),
        "validation_error",
    ),
    "negative_amount": (
        mut(lambda d: d["bill_lines"][0]["amount"].update(amount="-1")),
        "validation_error",
    ),
    "bad_claim_ref": (mut(lambda d: d.update(claim_ref="X-1")), "validation_error"),
    "too_many_lines": (big(2001), "validation_error"),
    "no_documents": (mut(lambda d: d.update(documents=[])), "validation_error"),
}
expected = {}
for n, (d, code) in invalid.items():
    dump(OUT / "invalid" / f"{n}.json", d)
    expected[n] = code
dump(OUT / "invalid" / "_expected.json", expected)
print(f"wrote {len(valid)} valid, {len(invalid)} invalid fixtures; DOC={DOC}")
