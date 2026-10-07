"""CalcInput / CalcResult (07 §2). Frozen, ``extra="forbid"``, money is ``Decimal`` — floats and >2dp are rejected."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from claim_contract.enums import AdmissionType, ClaimType
from claim_contract.models import Deduction
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from .rules_schema import PolicyRules


def _amount(v: Any) -> Decimal:
    if isinstance(v, (bool, float)):
        raise ValueError("floats are not allowed; send money as a string")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("not a valid decimal") from exc
    if not d.is_finite():
        raise ValueError("NaN/Infinity not allowed")
    if d < 0:
        raise ValueError("negative amounts are not allowed")
    if d != d.quantize(Decimal("0.01")):
        raise ValueError("more than 2 decimal places (never rounded silently)")
    return d.quantize(Decimal("0.01"))


def _qty(v: Any) -> Decimal:
    if isinstance(v, (bool, float)):
        raise ValueError("floats are not allowed")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("not a valid decimal") from exc
    if not d.is_finite() or d < 0:
        raise ValueError("invalid qty")
    return d


def _fmt(d: Decimal) -> str:
    return f"{d:.2f}"


Amount = Annotated[Decimal, BeforeValidator(_amount), PlainSerializer(_fmt, return_type=str)]
OutAmount = Annotated[Decimal, PlainSerializer(_fmt, return_type=str)]
Qty = Annotated[Decimal, BeforeValidator(_qty), PlainSerializer(lambda d: format(d, "f"), return_type=str)]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MappedGroup(StrEnum):
    room_rent = "room_rent"
    icu = "icu"
    nursing = "nursing"
    doctor_fees = "doctor_fees"
    surgeon_fees = "surgeon_fees"
    anaesthesia = "anaesthesia"
    ot_charges = "ot_charges"
    implant = "implant"
    medicine = "medicine"
    consumable = "consumable"
    investigation = "investigation"
    procedure_package = "procedure_package"
    ambulance = "ambulance"
    pre_hospitalisation = "pre_hospitalisation"
    post_hospitalisation = "post_hospitalisation"
    non_medical = "non_medical"
    other = "other"


class BlockedReason(StrEnum):
    POLICY_INACTIVE = "POLICY_INACTIVE"
    PREMIUM_LAPSED = "PREMIUM_LAPSED"
    OUTSIDE_COVER = "OUTSIDE_COVER"
    INITIAL_WAITING = "INITIAL_WAITING"
    PRE_EXISTING_WAITING = "PRE_EXISTING_WAITING"
    SPECIFIC_WAITING = "SPECIFIC_WAITING"
    SUM_INSURED_EXHAUSTED = "SUM_INSURED_EXHAUSTED"
    ALL_LINES_EXCLUDED = "ALL_LINES_EXCLUDED"


class FlagCode(StrEnum):
    SUM_INSURED_CAPPED = "SUM_INSURED_CAPPED"
    WAITING_PERIOD_HIT = "WAITING_PERIOD_HIT"
    UNMAPPED_LINE = "UNMAPPED_LINE"
    NO_DATE_ON_LINE = "NO_DATE_ON_LINE"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    PREMIUM_IN_GRACE = "PREMIUM_IN_GRACE"
    DEGRADED_MAPPING = "DEGRADED_MAPPING"
    ROOM_DAYS_ASSUMED = "ROOM_DAYS_ASSUMED"
    CO_PAY_SELECTED = "CO_PAY_SELECTED"
    ORDER_PROFILE_NON_DEFAULT = "ORDER_PROFILE_NON_DEFAULT"


class PreExisting(_M):
    icd_prefix: str
    declared_on: date | None = None


class PolicySnapshot(_M):
    policy_number: str
    product_code: str
    status: Literal["active", "lapsed", "cancelled", "suspended"]
    start_date: date
    end_date: date
    premium_paid_until: date
    grace_days: int = Field(default=30, ge=0)
    sum_insured: Amount
    bonus_sum: Amount = Decimal("0.00")
    utilised_this_year: Amount = Decimal("0.00")
    sum_insured_basis: Literal["individual", "floater"] = "individual"


class MemberSnapshot(_M):
    member_id: str
    relationship: Literal["self", "spouse", "child", "parent", "other"] = "self"
    dob: date
    cover_start: date
    pre_existing: list[PreExisting] = Field(default_factory=list)


class HospitalFacts(_M):
    hospital_id: str
    network_status: Literal["network", "non_network"]
    room_rent_tier: Literal["A", "B", "C"] | None = None


class AdmissionFacts(_M):
    admitted_on: date
    discharged_on: date
    admission_type: AdmissionType
    diagnosis_codes: list[str] = Field(default_factory=list)
    procedure_codes: list[str] = Field(default_factory=list)
    procedure_group: str | None = None
    day_care: bool = False
    hospital: HospitalFacts

    @model_validator(mode="after")
    def _dates(self) -> AdmissionFacts:
        if self.discharged_on < self.admitted_on:
            raise ValueError("discharged_on before admitted_on")
        return self


class CalcLine(_M):
    line_ref: str
    category: str
    mapped_group: MappedGroup
    procedure_group: str | None = None
    description: str = ""
    qty: Qty = Decimal("1")
    unit_price: Amount = Decimal("0.00")
    claimed_amount: Amount
    service_date: date | None = None
    room_type: Literal["ward", "semi_private", "single", "deluxe", "icu"] | None = None
    days: int | None = Field(default=None, ge=0)
    is_non_medical: bool = False
    is_implant: bool = False
    exclusion_tags: list[str] = Field(default_factory=list)
    mapping_source: Literal["rule", "agent"] = "rule"
    source_doc_id: UUID | None = None
    source_page: int | None = None


class CalcInput(_M):
    case_id: UUID
    claim_type: ClaimType
    admission_type: AdmissionType
    policy: PolicySnapshot
    member: MemberSnapshot
    admission: AdmissionFacts
    lines: Annotated[list[CalcLine], Field(min_length=1)]
    rules: PolicyRules
    rules_version: int = Field(ge=1)
    advance_paid: Amount = Decimal("0.00")
    declared_total: Amount | None = None  # submitted claimed total; mismatch only raises TOTAL_MISMATCH

    @field_validator("lines")
    @classmethod
    def _unique(cls, v: list[CalcLine]) -> list[CalcLine]:
        refs = [ln.line_ref for ln in v]
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate line_ref")
        return v


# ----------------------------------------------------------------------------------------------- output
class LineRuleHit(_M):
    rule_id: str
    step: str
    amount: OutAmount
    explanation: str


class LineResult(_M):
    line_ref: str
    claimed: OutAmount
    disallowed: OutAmount
    allowed_after_caps: OutAmount
    deductible_share: OutAmount
    co_pay_share: OutAmount
    sum_insured_cut: OutAmount
    payable: OutAmount
    rule_trace: list[LineRuleHit]


class CalcFlag(_M):
    code: FlagCode
    message: str
    line_ref: str | None = None


class TraceStep(_M):
    step: str
    rule_id: str | None = None
    description: str
    before_total: OutAmount
    after_total: OutAmount
    affected_lines: list[str] = Field(default_factory=list)


class CalcResult(_M):
    engine_version: str
    rules_version: int
    claimed_total: OutAmount
    eligible_total: OutAmount
    payable_total: OutAmount
    patient_pays_total: OutAmount
    lines: list[LineResult]
    summary_deductions: list[Deduction]
    flags: list[CalcFlag]
    trace: list[TraceStep]
    blocked: BlockedReason | None = None
    remaining_sum_insured_before: OutAmount
    remaining_sum_insured_after: OutAmount

    def flag_codes(self) -> list[str]:
        return [f.code.value for f in self.flags]
