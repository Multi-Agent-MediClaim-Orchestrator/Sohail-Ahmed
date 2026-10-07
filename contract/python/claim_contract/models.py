"""Cross-boundary models (01-02 section 5). Money is Decimal serialised as a string."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    field_validator,
    model_validator,
)

from claim_contract.enums import (
    AdmissionType,
    BillCategory,
    ClaimType,
    DecisionOutcome,
    DocSupplementReason,
    DocType,
    Gender,
    HospitalCaseStatus,
    InsurerCaseStatus,
    QueryCategory,
    QueryStatus,
    SettlementMode,
    WithdrawReason,
)

CENT = Decimal("0.01")
AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a JSON-shaped dict (strings for dates, UUIDs and decimals)."""
        return cls.model_validate(data, strict=False)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)


def _to_decimal(v: Any) -> Decimal:
    if isinstance(v, float):
        raise ValueError("float is not allowed for money/quantities; send a string")
    if isinstance(v, bool):
        raise ValueError("invalid decimal")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as e:
        raise ValueError("invalid decimal") from e
    if not d.is_finite():
        raise ValueError("NaN/Inf not allowed")
    return d


def _quantize(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


Amount = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    AfterValidator(_quantize),
    Field(ge=0, max_digits=14),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]
Quantity = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    Field(gt=0, decimal_places=3),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]


class Money(ContractModel):
    amount: Amount
    currency: Literal["INR"] = "INR"

    @classmethod
    def of(cls, value: str | int | Decimal) -> Money:
        return cls(amount=_to_decimal(value))


ZERO = Money.of("0")


def _check_text(s: str | None) -> str | None:
    if s is not None and (AADHAAR_RE.search(s) or PAN_RE.search(s)):
        raise ValueError("raw government id pattern found in free text (V-09)")
    return s


class Patient(ContractModel):
    full_name: str = Field(min_length=2, max_length=120)
    dob: date
    gender: Gender
    member_id: str = Field(min_length=4, max_length=32, pattern=r"^[A-Z0-9\-]+$")
    policy_number: str = Field(min_length=4, max_length=40)
    id_proof_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    contact_masked: str | None = Field(default=None, max_length=20)

    @field_validator("dob")
    @classmethod
    def _dob(cls, v: date) -> date:
        today = datetime.now(UTC).date()
        if v > today or (today.year - v.year) > 120:
            raise ValueError("dob must not be in the future and age must be <= 120")
        return v

    _t = field_validator("full_name", "contact_masked")(_check_text)


class Admission(ContractModel):
    admission_type: AdmissionType
    admitted_on: date
    discharged_on: date
    diagnosis_codes: list[str] = Field(min_length=1, max_length=10)
    procedure_codes: list[str] = Field(default_factory=list, max_length=10)
    treating_doctor: str = Field(min_length=2, max_length=120)
    hospital_id: str = Field(pattern=r"^HOSP-\d{4}$")
    preauth_ref: str | None = None
    length_of_stay_days: int | None = None

    @field_validator("diagnosis_codes")
    @classmethod
    def _icd(cls, v: list[str]) -> list[str]:
        for c in v:
            if not ICD10_RE.match(c):
                raise ValueError(f"malformed ICD-10 code {c!r}")
        return v

    @model_validator(mode="after")
    def _dates(self) -> Admission:
        today = datetime.now(UTC).date()
        if self.discharged_on < self.admitted_on:
            raise ValueError("discharged_on must be >= admitted_on (V-03)")
        if self.discharged_on > today or self.admitted_on > today:
            raise ValueError("dates must not be in the future")
        los = (self.discharged_on - self.admitted_on).days
        if self.length_of_stay_days is not None and self.length_of_stay_days != los:
            raise ValueError("length_of_stay_days does not match dates")
        return self


class BillLine(ContractModel):
    line_id: str | None = Field(
        default=None, min_length=1, max_length=16
    )  # receiver assigns L001.. when absent
    code: str | None = None
    description: str = Field(min_length=2, max_length=200)
    category: BillCategory
    qty: Quantity
    unit_price: Money
    amount: Money
    service_date: date | None = None
    source_doc_id: UUID
    source_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _amount(self) -> BillLine:
        expected = _quantize(self.qty * self.unit_price.amount)
        if abs(expected - self.amount.amount) > CENT:
            raise ValueError("totals_mismatch: amount != qty x unit_price")
        return self

    _t = field_validator("description")(_check_text)


class ClaimTotals(ContractModel):
    gross: Money
    discounts: Money
    claimed: Money
    patient_paid_advance: Money | None = None

    @model_validator(mode="after")
    def _claimed(self) -> ClaimTotals:
        if self.claimed.amount != self.gross.amount - self.discounts.amount:
            raise ValueError("totals_mismatch: claimed != gross - discounts (V-02)")
        return self


class DocumentRef(ContractModel):
    doc_id: UUID
    doc_type: DocType
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=25_000_000)
    mime_type: Literal["application/pdf", "image/jpeg", "image/png", "image/tiff"] | None = None
    download_url: HttpUrl
    url_expires_at: datetime | None = None
    parse_confidence: float | None = Field(default=None, ge=0, le=1)  # not money
    pages: int = Field(ge=1, le=300)
    received_via: Literal["upload", "scan", "email", "supplement"] = "upload"


class ConfigVersions(ContractModel):
    doc_requirements: int = Field(ge=1)
    deadlines: int = Field(ge=1)
    router_rules: int = Field(ge=1)
    confidence_gates: int = Field(ge=1)


class Citation(ContractModel):
    source_id: str
    clause: str | None = None
    page: int | None = None
    snippet: str = Field(max_length=300)


class Deduction(ContractModel):
    line_ref: str
    rule_id: str
    amount: Money
    explanation: str = Field(max_length=500)


class Decision(ContractModel):
    outcome: DecisionOutcome
    approved_amount: Money
    deductions: list[Deduction] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    reviewer_ids: list[str] = Field(min_length=1, max_length=3)
    calc_trace_id: UUID
    policy_version: int = Field(ge=1)
    decided_at: datetime
    supersedes: int | None = None  # v1.1 reserved

    @model_validator(mode="after")
    def _rules(self) -> Decision:
        if self.outcome is DecisionOutcome.REJECT and self.approved_amount.amount != 0:
            raise ValueError("reject requires approved_amount == 0 (V-12)")
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer_ids must be distinct")
        return self

    def reconciles_with(self, claimed: Money) -> bool:
        """sum(deductions) + approved == claimed for approve/partial (checked by receiver)."""
        if self.outcome not in (DecisionOutcome.APPROVE, DecisionOutcome.PARTIAL):
            return True
        total = sum((d.amount.amount for d in self.deductions), Decimal("0.00"))
        return total + self.approved_amount.amount == claimed.amount


class SettlementNotice(ContractModel):
    settlement_id: UUID
    amount: Money
    utr: str = Field(min_length=6, max_length=64)
    paid_on: date
    mode: SettlementMode
    tds: Money
    status: Literal["paid", "reversed"] = "paid"  # 1.1: a reversal notice (insurer 03-06)


class ClaimSubmission(ContractModel):
    contract_version: str = Field(pattern=r"^\d+\.\d+$")
    claim_ref: str = Field(pattern=r"^HC-\d{4}-\d{6}$")
    claim_type: ClaimType
    patient: Patient
    admission: Admission
    bill_lines: list[BillLine] = Field(min_length=1, max_length=2000)
    totals: ClaimTotals
    documents: list[DocumentRef] = Field(min_length=1, max_length=100)
    config_versions: ConfigVersions
    journey_id: UUID | None = None  # v1.1
    submitted_at: datetime
    hospital_notes: str | None = Field(default=None, max_length=2000)

    _t = field_validator("hospital_notes")(_check_text)

    @model_validator(mode="after")
    def _cross(self) -> ClaimSubmission:
        total = sum((ln.amount.amount for ln in self.bill_lines), Decimal("0.00"))
        if total != self.totals.gross.amount:
            raise ValueError("totals_mismatch: sum(bill_lines) != totals.gross (V-01)")
        ids = [ln.line_id for ln in self.bill_lines if ln.line_id is not None]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate line_id (V-07)")
        doc_ids = {d.doc_id for d in self.documents}
        adm = self.admission
        for ln in self.bill_lines:
            if ln.source_doc_id not in doc_ids:
                raise ValueError(f"source_doc_id of {ln.line_id} not in documents (V-04)")
            if ln.service_date and not (adm.admitted_on <= ln.service_date <= adm.discharged_on):
                raise ValueError(f"service_date of {ln.line_id} outside admission (V-06)")
        if (
            self.claim_type is ClaimType.CASHLESS
            and adm.admission_type is AdmissionType.PLANNED
            and not adm.preauth_ref
        ):
            raise ValueError("planned cashless claims require preauth_ref (V-05)")
        return self


class Acknowledgement(ContractModel):
    claim_ref: str
    insurer_claim_no: str = Field(pattern=r"^IC-\d{4}-\d{6}$")
    status: InsurerCaseStatus
    received_at: datetime
    sequence: int = Field(ge=0)
    document_ingest: dict[str, int] = Field(default_factory=lambda: {"queued": 0, "failed": 0})
    contract_version: str | None = None


class StatusUpdate(ContractModel):
    claim_ref: str
    insurer_claim_no: str
    status: InsurerCaseStatus
    hospital_visible_status: HospitalCaseStatus
    sequence: int = Field(ge=0)
    occurred_at: datetime
    note: str | None = None
    open_query_ids: list[UUID] = Field(default_factory=list)
    decision: Decision | None = None


class Query(ContractModel):
    query_id: UUID
    round: int = Field(ge=1, le=3)
    category: QueryCategory
    text: str = Field(min_length=10, max_length=4000)
    requested_doc_types: list[DocType] = Field(default_factory=list)
    due_by: datetime
    status: QueryStatus
    raised_by: str
    grounding: list[Citation] = Field(default_factory=list)


class QueryResponse(ContractModel):
    query_id: UUID
    answer_text: str = Field(default="", max_length=8000)
    attached_doc_ids: list[UUID] = Field(default_factory=list, max_length=20)
    responded_by: str
    responded_at: datetime

    @model_validator(mode="after")
    def _nonempty(self) -> QueryResponse:
        if not self.answer_text.strip() and not self.attached_doc_ids:
            raise ValueError("need answer_text or attached_doc_ids")
        return self


class WithdrawRequest(ContractModel):
    reason: WithdrawReason
    note: str | None = Field(default=None, max_length=1000)


class DocSupplement(ContractModel):
    reason: DocSupplementReason
    query_id: UUID | None = None
    documents: list[DocumentRef] = Field(min_length=1, max_length=100)


class DocRefreshRequest(ContractModel):
    claim_ref: str
    reason: Literal["url_expired"] = "url_expired"


class DocRefreshResponse(ContractModel):
    doc_id: UUID
    download_url: HttpUrl
    expires_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)


class QueryCallback(ContractModel):
    claim_ref: str = Field(pattern=r"^HC-\d{4}-\d{6}$")
    sequence: int = Field(ge=0)
    query: Query


class DecisionCallback(ContractModel):
    claim_ref: str = Field(pattern=r"^HC-\d{4}-\d{6}$")
    sequence: int = Field(ge=0)
    decision: Decision


class SettlementCallback(ContractModel):
    claim_ref: str = Field(pattern=r"^HC-\d{4}-\d{6}$")
    sequence: int = Field(ge=0)
    settlement: SettlementNotice


class HealthResponse(ContractModel):
    status: Literal["ok", "degraded"]
    version: str
    time: datetime


class ContractInfo(ContractModel):
    supported: list[str]
    default: str
    deprecated: list[str] = Field(default_factory=list)


def error_code_for(message: str) -> str:
    """Map a validator message to a contract error code (totals_mismatch or validation_error)."""
    return "totals_mismatch" if "totals_mismatch" in message else "validation_error"
