"""Pure-data verification context. Built from the DB by ``services/context.py``; consumed by the pure ``check_*``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..config.schemas import DocRequirements, PolicyRulesPayload, Thresholds


@dataclass(frozen=True)
class HospitalInfo:
    code: str
    network_status: str
    empanelment_valid_till: date | None = None
    watchlist: bool = False


@dataclass(frozen=True)
class PatientInfo:
    full_name: str
    dob: date
    gender: str
    member_id: str
    policy_number: str
    id_proof_hash: str | None = None


@dataclass(frozen=True)
class PolicyInfo:
    status: str
    start_date: date
    end_date: date
    premium_paid_until: date | None
    grace_days: int
    sum_insured: Decimal
    bonus: Decimal
    product_code: str


@dataclass(frozen=True)
class MemberInfo:
    full_name: str
    dob: date
    gender: str
    id_proof_hash: str | None
    cover_start: date
    pre_existing: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocInfo:
    id: str
    doc_type: str
    pages: int
    sha256: str
    fetch_status: str
    parse_confidence: float | None = None
    superseded: bool = False
    vision: dict[str, Any] | None = None  # {stamp_detected, signature_detected, tamper_score, ...} from vision-service
    extract: dict[str, Any] | None = None  # typed, masked extraction: {"total": "...", "dates": [...], "signed": bool}


@dataclass(frozen=True)
class Overlap:
    case_id: str
    claim_no: str
    status: str


@dataclass
class VCtx:
    case_id: str
    claim_type: str
    admission_type: str
    admitted_on: date
    discharged_on: date
    claimed_amount: Decimal
    claimed_gross: Decimal
    diagnosis_codes: list[str]
    procedure_codes: list[str]
    procedure_group: str | None
    hospital: HospitalInfo
    patient: PatientInfo
    policy: PolicyInfo | None
    member: MemberInfo | None
    utilised: Decimal
    documents: list[DocInfo]
    bill_categories: set[str]
    docs_cfg: DocRequirements
    thresholds: Thresholds
    rules: PolicyRulesPayload | None
    overlapping: list[Overlap] = field(default_factory=list)
    shared_hash_docs: list[str] = field(default_factory=list)
    config_versions: dict[str, Any] = field(default_factory=dict)

    @property
    def active_docs(self) -> list[DocInfo]:
        return [d for d in self.documents if not d.superseded]

    @property
    def fetched_docs(self) -> list[DocInfo]:
        return [d for d in self.active_docs if d.fetch_status == "fetched"]

    @property
    def is_accident(self) -> bool:
        if any(c[:1] in ("S", "T", "V", "W", "X", "Y") for c in self.diagnosis_codes):
            return True
        return self.admission_type == "emergency" and any(d.doc_type == "fir_mlc" for d in self.active_docs)
