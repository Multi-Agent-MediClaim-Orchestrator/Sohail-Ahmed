"""Strict draft payload the Claim Builder returns (doc 06 §3, §5.2). Crews never write the DB; the API parses
their output with this model (extra="forbid") and persists it."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from claim_contract.enums import AdmissionType, BillCategory
from claim_contract.models import Amount, Quantity
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DraftPatient(Strict):
    full_name: str = Field(min_length=2, max_length=120)
    dob: date
    gender: Literal["M", "F", "O"]
    member_id: str = Field(min_length=1, max_length=40)
    policy_number: str = Field(min_length=1, max_length=40)


class DraftAdmission(Strict):
    admission_type: AdmissionType
    admitted_on: date
    discharged_on: date
    diagnosis_codes: list[str] = Field(default_factory=list, max_length=10)
    procedure_codes: list[str] = Field(default_factory=list, max_length=10)
    treating_doctor: str = Field(default="", max_length=120)
    hospital_id: str | None = None
    preauth_ref: str | None = None


class DraftLine(Strict):
    line_no: int = Field(ge=1, le=2000)
    code: str | None = Field(default=None, max_length=40)
    description: str = Field(min_length=2, max_length=200)
    category: BillCategory
    qty: Quantity
    unit_price: Amount
    amount: Amount
    service_date: date | None = None
    source_doc_id: UUID
    source_page: int | None = Field(default=None, ge=1)


class DraftTotals(Strict):
    gross: Amount
    discounts: Amount
    claimed: Amount


class ClaimDraftIn(Strict):
    patient: DraftPatient
    admission: DraftAdmission
    bill_lines: list[DraftLine] = Field(min_length=1, max_length=2000)
    totals: DraftTotals
    documents: list[UUID] = Field(default_factory=list, max_length=100)


class Provenance(Strict):
    doc_id: UUID
    page: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)


class DraftResult(Strict):
    """Body of POST /v1/internal/cases/{id}/claim/draft."""

    job_id: str = Field(min_length=1, max_length=64)
    payload: ClaimDraftIn
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    model_info: dict[str, str | int | float | None] = Field(default_factory=dict)
    repair_round: int = Field(default=0, ge=0, le=2)
