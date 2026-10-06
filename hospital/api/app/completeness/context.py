"""Everything `evaluate` may read. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


@dataclass(frozen=True)
class DocFacts:
    doc_id: str
    doc_type: str
    usable_state: Literal["ok", "excluded", "pending_processing"]
    uploaded_at: datetime
    quality_flags: frozenset[str] = frozenset()
    quality_score: float = 1.0
    has_required_stamp: bool | None = None
    stamp_confidence: float | None = None
    parse_confidence: float | None = None
    agreement_score: float | None = None
    classification_confidence: float | None = None
    typed_json: dict[str, Any] | None = None
    page_count: int = 1
    excluded_reason: str | None = None  # infected | deleted | superseded


@dataclass(frozen=True)
class PatientFacts:
    name: str
    dob: date | None = None
    member_id: str | None = None
    policy_number: str | None = None


@dataclass(frozen=True)
class AdmissionFacts:
    admitted_on: date | None = None
    discharged_on: date | None = None
    diagnosis_codes: tuple[str, ...] = ()
    procedure_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WaiverFacts:
    rule_id: str
    reason: str


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    claim_type: str
    admission_type: str
    procedure_group: str | None
    flags: frozenset[str]
    patient: PatientFacts
    admission: AdmissionFacts
    docs_by_type: dict[str, list[DocFacts]]
    now: datetime
    waivers: dict[str, WaiverFacts] = field(default_factory=dict)
    gates: dict[str, Any] = field(default_factory=lambda: {"agreement_min": 0.9, "parse_min": 0.75})
    unclassified_ids: tuple[str, ...] = ()
    name_match_min: int = 90
    stamp_grace_s: int = 600  # how long a missing stamp evaluation is "still pending"
