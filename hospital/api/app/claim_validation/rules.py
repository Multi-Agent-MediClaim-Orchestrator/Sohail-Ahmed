"""Deterministic claim validators V01-V12 (doc 06 §5.1). Money is Decimal, never float. The agent proposes;
these decide whether a draft may be signed off: errors block, warnings need acknowledgement."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from rapidfuzz import fuzz

from app.claim_validation.schemas import ClaimDraftIn

CENT = Decimal("0.01")
ICD10 = re.compile(r"^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$")
BILL_TYPES = {"final_bill", "itemised_bill", "pharmacy_bill", "procedure_bill"}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # error | warning
    field: str
    message: str
    ack_required: bool = False


@dataclass(frozen=True)
class DocInfo:
    doc_id: str
    pages: int
    clean: bool  # active and virus-clean
    doc_type: str | None = None
    bill_total: Decimal | None = None


@dataclass(frozen=True)
class CaseFacts:
    claim_ref: str
    pipeline: str  # cashless | reimbursement
    full_name: str
    dob: date | None
    admitted_on: date | None
    discharged_on: date | None
    preauth_amount: Decimal | None = None


@dataclass(frozen=True)
class ValidationContext:
    case: CaseFacts
    docs: dict[str, DocInfo]
    room_per_day_max: Decimal = Decimal("25000")
    name_error_below: int = 80
    name_ok_from: int = 90


@dataclass
class ValidationResult:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    reconciliation: dict[str, str | None] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def to_json(self) -> dict[str, Any]:
        return {
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "reconciliation": self.reconciliation,
        }


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    return format(q2(d), "f")


def lines_sum(draft: ClaimDraftIn) -> Decimal:
    return sum((ln.amount for ln in draft.bill_lines), Decimal("0"))


def v01(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    s, gross = lines_sum(d), d.totals.gross
    if s != gross:
        return [
            Finding(
                "V01",
                "error",
                "totals.gross",
                f"Line items total {_money(s)} but gross is {_money(gross)} (diff {_money(s - gross)}).",
            )
        ]
    return []


def v02(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    x = d.totals.gross - d.totals.discounts
    if x != d.totals.claimed:
        return [
            Finding(
                "V02",
                "error",
                "totals.claimed",
                f"Gross - discounts = {_money(x)}, claimed shows {_money(d.totals.claimed)}.",
            )
        ]
    return []


def v03(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    for i, ln in enumerate(d.bill_lines):
        calc = ln.qty * ln.unit_price
        if abs(calc - ln.amount) > CENT:
            out.append(
                Finding(
                    "V03",
                    "error",
                    f"bill_lines[{i}].amount",
                    f"Line {ln.line_no}: {ln.qty} x {_money(ln.unit_price)} = {_money(calc)}, shown {_money(ln.amount)}.",
                )
            )
    return out


def v04(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    for i, ln in enumerate(d.bill_lines):
        doc = ctx.docs.get(str(ln.source_doc_id))
        if doc is None or not doc.clean:
            out.append(
                Finding(
                    "V04",
                    "error",
                    f"bill_lines[{i}].source_doc_id",
                    f"Line {ln.line_no} points to a document that is not part of this case.",
                )
            )
        elif ln.source_page is not None and ln.source_page > doc.pages:
            out.append(
                Finding(
                    "V04",
                    "error",
                    f"bill_lines[{i}].source_page",
                    f"Line {ln.line_no} cites page {ln.source_page} of a {doc.pages}-page document.",
                )
            )
    return out


def v05(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    c = ctx.case
    if c.admitted_on and d.admission.admitted_on != c.admitted_on:
        out.append(
            Finding(
                "V05",
                "error",
                "admission.admitted_on",
                "Admission date differs from the case record.",
            )
        )
    if c.discharged_on and d.admission.discharged_on != c.discharged_on:
        out.append(
            Finding(
                "V05",
                "error",
                "admission.discharged_on",
                "Discharge date differs from the case record.",
            )
        )
    lo = d.admission.admitted_on - timedelta(days=1)
    hi = d.admission.discharged_on + timedelta(days=1)
    for i, ln in enumerate(d.bill_lines):
        if ln.service_date and not (lo <= ln.service_date <= hi):
            out.append(
                Finding(
                    "V05",
                    "error",
                    f"bill_lines[{i}].service_date",
                    f"Line {ln.line_no} is dated outside the admission window.",
                )
            )
    return out


def _norm_name(s: str) -> str:
    return " ".join(re.sub(r"[^a-z\s]", " ", s.lower()).split())


def v06(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    c = ctx.case
    if c.dob and d.patient.dob != c.dob:
        out.append(
            Finding("V06", "error", "patient.dob", "Date of birth differs from the patient record.")
        )
    r = int(fuzz.token_sort_ratio(_norm_name(d.patient.full_name), _norm_name(c.full_name)))
    if r < ctx.name_error_below:
        out.append(
            Finding(
                "V06",
                "error",
                "patient.full_name",
                "Patient name does not match the patient record.",
            )
        )
    elif r < ctx.name_ok_from:
        out.append(
            Finding(
                "V06",
                "warning",
                "patient.full_name",
                "Patient name differs slightly from the patient record.",
            )
        )
    return out


def v07(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    for i, code in enumerate(d.admission.diagnosis_codes):
        if not ICD10.match(code):
            out.append(
                Finding(
                    "V07",
                    "warning",
                    f"admission.diagnosis_codes[{i}]",
                    f"{code!r} is not a valid ICD-10 code.",
                )
            )
    for i, code in enumerate(d.admission.procedure_codes):
        if not code or len(code) > 12:
            out.append(
                Finding(
                    "V07",
                    "warning",
                    f"admission.procedure_codes[{i}]",
                    f"{code!r} is not a valid procedure code.",
                )
            )
    return out


def v08(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    by_doc: dict[str, Decimal] = {}
    for ln in d.bill_lines:
        by_doc[str(ln.source_doc_id)] = by_doc.get(str(ln.source_doc_id), Decimal("0")) + ln.amount
    for doc_id, total in sorted(by_doc.items()):
        info = ctx.docs.get(doc_id)
        if (
            info
            and info.doc_type in BILL_TYPES
            and info.bill_total is not None
            and abs(info.bill_total - total) > CENT
        ):
            out.append(
                Finding(
                    "V08",
                    "error",
                    "totals.gross",
                    f"Bill document total {_money(info.bill_total)} does not match its claim lines {_money(total)}.",
                )
            )
    return out


def v09(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    seen: dict[tuple[Any, ...], int] = {}
    out = []
    for i, ln in enumerate(d.bill_lines):
        key = (ln.code, ln.description.strip().lower(), ln.service_date, ln.amount)
        if key in seen:
            out.append(
                Finding(
                    "V09",
                    "warning",
                    f"bill_lines[{i}]",
                    f"Possible duplicate of line {d.bill_lines[seen[key]].line_no}.",
                    ack_required=True,
                )
            )
        else:
            seen[key] = i
    return out


def v10(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    p = ctx.case.preauth_amount
    if ctx.case.pipeline == "cashless" and p is not None and d.totals.claimed > p * Decimal("1.10"):
        over = ((d.totals.claimed - p) / p * 100).quantize(Decimal("1"))
        return [
            Finding(
                "V10",
                "warning",
                "totals.claimed",
                f"Claimed {_money(d.totals.claimed)} exceeds pre-auth {_money(p)} by {over}%.",
                ack_required=True,
            )
        ]
    return []


def v11(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    return [
        Finding(
            "V11",
            "warning",
            f"bill_lines[{i}].unit_price",
            f"Line {ln.line_no}: room rent {_money(ln.unit_price)} per day exceeds {_money(ctx.room_per_day_max)}.",
        )
        for i, ln in enumerate(d.bill_lines)
        if ln.category.value == "room" and ln.unit_price > ctx.room_per_day_max
    ]


def v12(d: ClaimDraftIn, ctx: ValidationContext) -> list[Finding]:
    out = []
    for i, doc_id in enumerate(d.documents):
        info = ctx.docs.get(str(doc_id))
        if info is None or not info.clean:
            out.append(
                Finding(
                    "V12",
                    "error",
                    f"documents[{i}]",
                    "A referenced document is not part of this case or is not clean.",
                )
            )
    return out


VALIDATORS = (v01, v02, v03, v04, v05, v06, v07, v08, v09, v10, v11, v12)


def validate(draft: ClaimDraftIn, ctx: ValidationContext) -> ValidationResult:
    res = ValidationResult()
    for fn in VALIDATORS:
        for f in fn(draft, ctx):
            (res.errors if f.severity == "error" else res.warnings).append(f)
    key = lambda f: (f.code, f.field)  # noqa: E731
    res.errors.sort(key=key)
    res.warnings.sort(key=key)
    totals = [
        i.bill_total
        for i in ctx.docs.values()
        if i.doc_type in BILL_TYPES
        and i.bill_total is not None
        and i.doc_id in {str(ln.source_doc_id) for ln in draft.bill_lines}
    ]
    s = lines_sum(draft)
    res.reconciliation = {
        "lines_sum": _money(s),
        "bill_total": _money(sum(totals, Decimal("0"))) if totals else None,
        "gross": _money(draft.totals.gross),
        "diff": _money(s - draft.totals.gross),
    }
    return res


def required_acknowledgements(result: dict[str, Any]) -> list[str]:
    return sorted({w["code"] for w in result.get("warnings", []) if w.get("ack_required")})


__all__ = ["UUID", "validate"]
