"""Cross-field business rules V-01..V-12 (01-02 §7) shared by hospital-api, insurer-api and tpa-sim.

``validate_submission`` returns a list of :class:`Violation`; callers decide how to map them (the insurer
door turns ``V-05`` into a warning, 02-api-claim-receipt V11). ``check_submission`` raises ``ProblemError``
with the contract error code of the *first* blocking violation (``totals_mismatch`` > ``validation_error``)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from claim_contract.enums import AdmissionType, ClaimType
from claim_contract.errors import FieldError, ProblemError
from claim_contract.models import ClaimSubmission, Decision

AADHAAR_RE = re.compile(r"(?<![\w-])\d{4}\s?\d{4}\s?\d{4}(?![\w-])")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
KNOWN_ICD_PREFIXES: frozenset[str] = frozenset()  # unknown-but-well-formed codes only warn (V-10)

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Violation:
    rule: str
    code: str  # contract error code
    field: str
    message: str
    severity: str = "error"  # error | warning


@dataclass
class ValidationReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _strings(obj: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (path, string) for every string leaf in a JSON-shaped structure."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def validate_submission(
    sub: ClaimSubmission, *, preauth_is_warning: bool = False, today: datetime | None = None
) -> ValidationReport:
    r = ValidationReport()
    add = r.violations.append

    # V-01 sum(lines) == gross
    total = sum((bl.amount.amount for bl in sub.bill_lines), Decimal("0.00"))
    if total != sub.totals.gross.amount:
        add(
            Violation(
                "V-01",
                "totals_mismatch",
                "totals.gross",
                f"Sum of bill_lines ({total:.2f}) differs from totals.gross ({sub.totals.gross.amount:.2f})",
            )
        )
    # V-02 claimed == gross - discounts
    expected_claimed = sub.totals.gross.amount - sub.totals.discounts.amount
    if expected_claimed != sub.totals.claimed.amount:
        add(
            Violation(
                "V-02",
                "totals_mismatch",
                "totals.claimed",
                f"claimed must equal gross - discounts ({expected_claimed:.2f})",
            )
        )
    adm = sub.admission
    # V-04 every source_doc_id in documents; V-07 duplicate line_id; V-06 service dates
    doc_ids = {d.doc_id for d in sub.documents}
    if len(doc_ids) != len(sub.documents):
        add(Violation("V-04", "validation_error", "documents", "duplicate doc_id in documents"))
    seen: set[str] = set()
    for i, bl in enumerate(sub.bill_lines):
        if bl.source_doc_id not in doc_ids:
            add(
                Violation(
                    "V-04",
                    "validation_error",
                    f"bill_lines[{i}].source_doc_id",
                    "unknown source document",
                )
            )
        if bl.line_id is not None:
            if bl.line_id in seen:
                add(
                    Violation(
                        "V-07", "validation_error", f"bill_lines[{i}].line_id", "duplicate line_id"
                    )
                )
            seen.add(bl.line_id)
        if bl.service_date is not None and not (
            adm.admitted_on <= bl.service_date <= adm.discharged_on
        ):
            add(
                Violation(
                    "V-06",
                    "validation_error",
                    f"bill_lines[{i}].service_date",
                    "service_date outside admission window",
                )
            )
    # V-05 cashless + planned requires preauth_ref
    if (
        sub.claim_type is ClaimType.cashless
        and adm.admission_type is AdmissionType.planned
        and not adm.preauth_ref
    ):
        add(
            Violation(
                "V-05",
                "validation_error",
                "admission.preauth_ref",
                "preauth_ref required for planned cashless admissions",
                "warning" if preauth_is_warning else "error",
            )
        )
    # V-09 raw ID patterns anywhere
    payload = sub.model_dump(mode="json")
    payload.get("patient", {}).pop("id_proof_hash", None)  # a 64-hex hash is allowed
    for path, s in _strings(payload):
        if path.startswith("documents") and path.endswith("sha256"):
            continue
        if path.endswith("download_url") or path.endswith("sha256") or UUID_RE.match(s):
            continue
        if AADHAAR_RE.search(s) or PAN_RE.search(s):
            add(Violation("V-09", "validation_error", path, "raw government ID pattern found"))
    # V-10 well-formed but unknown ICD codes: warning only (malformed are rejected by the model)
    if KNOWN_ICD_PREFIXES:
        for i, code in enumerate(adm.diagnosis_codes):
            if code[:3] not in KNOWN_ICD_PREFIXES:
                add(
                    Violation(
                        "V-10",
                        "validation_error",
                        f"admission.diagnosis_codes[{i}]",
                        "unknown ICD-10 code",
                        "warning",
                    )
                )
    return r


def check_submission(sub: ClaimSubmission, **kw: object) -> ValidationReport:
    """Raise ``ProblemError`` if any error-level violation exists; otherwise return the report (warnings)."""
    report = validate_submission(sub, **kw)  # type: ignore[arg-type]
    if report.errors:
        code = (
            "totals_mismatch"
            if any(v.code == "totals_mismatch" for v in report.errors)
            else "validation_error"
        )
        raise ProblemError(
            code,
            report.errors[0].message,
            errors=[FieldError(field=v.field, message=v.message) for v in report.errors],
        )
    return report


def validate_decision_amounts(decision: Decision, claimed: Decimal) -> list[Violation]:
    """V-11/V-12 and amount reconciliation; ``claimed`` comes from the stored claim (receiver-side check)."""
    out: list[Violation] = []
    if decision.approved_amount.amount > claimed:
        out.append(
            Violation(
                "V-DEC", "validation_error", "approved_amount", "approved_amount exceeds claimed"
            )
        )
    if decision.outcome.value in ("approve", "partial"):
        total = decision.approved_amount.amount + sum(
            (d.amount.amount for d in decision.deductions), Decimal("0.00")
        )
        if abs(total - claimed) > CENT:
            out.append(
                Violation(
                    "V-DEC",
                    "validation_error",
                    "deductions",
                    f"approved_amount + deductions ({total:.2f}) must equal claimed ({claimed:.2f})",
                )
            )
    return out


def require_distinct_reviewers(
    decision: Decision, amount: Decimal, t_four: Decimal
) -> list[Violation]:
    """V-11: two distinct reviewer ids when the decision amount exceeds T_four."""
    if amount > t_four and len(set(decision.reviewer_ids)) < 2:
        return [
            Violation(
                "V-11",
                "validation_error",
                "reviewer_ids",
                "two distinct reviewers required above T_four",
            )
        ]
    return []


def today_utc() -> datetime:
    return datetime.now(UTC)
