from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from claim_contract.enums import AdmissionType, ClaimType
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PatientIn(Strict):
    uhid: str = Field(min_length=1, max_length=40)
    full_name: str = Field(min_length=2, max_length=120)
    dob: date
    gender: Literal["M", "F", "O"]
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("dob")
    @classmethod
    def _dob(cls, v: date) -> date:
        today = date.today()
        if v > today or v < today - timedelta(days=366 * 120):
            raise ValueError("dob must not be in the future and age must be <= 120")
        return v


class PolicyIn(Strict):
    insurer_name: str = Field(min_length=1, max_length=64)
    policy_number: str = Field(min_length=1, max_length=64)
    member_id: str = Field(min_length=1, max_length=64)


class CaseCreate(Strict):
    patient: PatientIn
    policy: PolicyIn
    claim_type: ClaimType
    admission_type: AdmissionType
    admitted_on: date | None = None
    discharged_on: date | None = None
    preauth_ref: str | None = Field(default=None, max_length=64)
    treating_doctor: str | None = Field(default=None, max_length=120)
    diagnosis_codes: list[str] = Field(default_factory=list, max_length=10)
    procedure_codes: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _dates(self) -> CaseCreate:
        today = date.today()
        if self.admitted_on and self.admitted_on > today:
            raise ValueError("admitted_on must not be in the future")
        if self.discharged_on:
            if self.discharged_on > today:
                raise ValueError("discharged_on must not be in the future")
            if self.admitted_on and self.discharged_on < self.admitted_on:
                raise ValueError("discharged_on must be on or after admitted_on")
        return self


class PatientPatch(Strict):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    dob: date | None = None
    gender: Literal["M", "F", "O"] | None = None
    phone: str | None = Field(default=None, max_length=20)


class PolicyPatch(Strict):
    insurer_name: str | None = Field(default=None, min_length=1, max_length=64)
    policy_number: str | None = Field(default=None, min_length=1, max_length=64)
    member_id: str | None = Field(default=None, min_length=1, max_length=64)


class CasePatch(Strict):
    admitted_on: date | None = None
    discharged_on: date | None = None
    diagnosis_codes: list[str] | None = Field(default=None, max_length=10)
    procedure_codes: list[str] | None = Field(default=None, max_length=10)
    treating_doctor: str | None = Field(default=None, max_length=120)
    preauth_ref: str | None = Field(default=None, max_length=64)
    claimed_amount: str | None = None
    patient: PatientPatch | None = None
    policy: PolicyPatch | None = None


class AssignBody(Strict):
    user_id: str | None


class TransitionBody(Strict):
    to: str
    reason: str | None = Field(default=None, max_length=500)
