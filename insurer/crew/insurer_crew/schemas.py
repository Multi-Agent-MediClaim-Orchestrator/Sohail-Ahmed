"""Request/context/output schemas (03-08 §3). Outputs are ``extra="forbid"``; there is deliberately NO confidence field anywhere
(hard rule 4) - a test introspects every schema for that.

Contexts accept both the document's shapes and the slimmer bundles insurer-api currently sends (``extra="allow"``)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Severity(StrEnum):
    info = "info"
    warning = "warning"
    blocker = "blocker"


SEV_RANK = {"info": 0, "warning": 1, "blocker": 2}


class IdentityIssue(StrEnum):
    NAME_VARIATION = "NAME_VARIATION"
    NAME_MISMATCH = "NAME_MISMATCH"
    DOB_MISMATCH = "DOB_MISMATCH"
    POLICY_NO_MISMATCH = "POLICY_NO_MISMATCH"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    PHOTO_ID_MISSING = "PHOTO_ID_MISSING"
    UNREADABLE_FIELD = "UNREADABLE_FIELD"


class AuthenticityIssue(StrEnum):
    FONT_INCONSISTENCY = "FONT_INCONSISTENCY"
    STAMP_MISSING = "STAMP_MISSING"
    STAMP_MISMATCH = "STAMP_MISMATCH"
    SIGNATURE_MISSING = "SIGNATURE_MISSING"
    ARITHMETIC_ERROR = "ARITHMETIC_ERROR"
    DUPLICATE_BILL = "DUPLICATE_BILL"
    DATE_ANOMALY = "DATE_ANOMALY"
    IMAGE_TAMPER_SUSPECTED = "IMAGE_TAMPER_SUSPECTED"
    TEMPLATE_UNKNOWN = "TEMPLATE_UNKNOWN"
    LOW_QUALITY = "LOW_QUALITY"


class MappedGroup(StrEnum):  # mirrors calc_engine.models.MappedGroup (07 §2)
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


class DocType(StrEnum):
    discharge_summary = "discharge_summary"
    final_bill = "final_bill"
    itemised_bill = "itemised_bill"
    claim_form = "claim_form"
    id_proof = "id_proof"
    policy_card = "policy_card"
    prescription = "prescription"
    investigation_report = "investigation_report"
    implant_sticker = "implant_sticker"
    pre_auth_form = "pre_auth_form"
    cancelled_cheque = "cancelled_cheque"
    other = "other"


# --------------------------------------------------------------------------------------------------
# request envelope
# --------------------------------------------------------------------------------------------------
class AgentOptions(Strict):
    model_alias: str | None = None
    max_tokens: int | None = Field(default=None, ge=64, le=4096)
    timeout_s: float | None = Field(default=None, ge=1, le=300)
    allow_degraded: bool = True


class AgentRequest(Strict):
    request_id: UUID
    case_id: UUID | str
    trace_parent: str | None = None
    context: dict[str, Any]
    options: AgentOptions = Field(default_factory=AgentOptions)


# --------------------------------------------------------------------------------------------------
# contexts
# --------------------------------------------------------------------------------------------------
class DocSummary(Loose):
    doc_id: str
    doc_type: str = "other"
    pages: int = 1
    parse_confidence: float = 1.0
    extract_masked: dict[str, Any] = Field(default_factory=dict)
    text_excerpt_masked: str = ""


class IdentityContext(Loose):
    member: dict[str, Any] | None = None
    patient: dict[str, Any] | None = None
    patient_name_variants: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_facts: dict[str, Any] = Field(default_factory=dict)
    docs: list[DocSummary] = Field(default_factory=list)


class AuthenticityContext(Loose):
    vision_reports: list[dict[str, Any]] = Field(default_factory=list)
    arithmetic: dict[str, Any] | None = None
    duplicates: dict[str, Any] | None = None
    stamp_reports: list[dict[str, Any]] = Field(default_factory=list)
    docs: list[DocSummary] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)  # insurer-api's slimmer form
    bill_lines: list[dict[str, Any]] = Field(default_factory=list)


class CoverageContext(Loose):
    policy: dict[str, Any] = Field(default_factory=dict)  # {product_code, effective_date}
    diagnoses: list[dict[str, Any]] = Field(default_factory=list)  # [{icd, name}]
    procedures: list[dict[str, Any]] = Field(default_factory=list)
    diagnosis_codes: list[str] = Field(default_factory=list)
    procedure_codes: list[str] = Field(default_factory=list)
    claim_type: str | None = None
    admission_type: str | None = None
    admitted_on: str | None = None


class RawBillLine(Loose):
    line_ref: str
    category: str = "other"
    description: str = ""
    qty: str | float | int = 1
    unit_price: str | float | int = 0
    amount: str | float | int = 0


class CalcMapContext(Loose):
    lines: list[RawBillLine]
    product_code: str = ""


class Finding(Loose):
    key: str
    kind: str = ""
    severity: str = "warning"
    detail: str = ""
    requested_doc_type: str | None = None
    line_ref: str | None = None


class Requirement(Loose):
    doc_type: str
    rule: str = ""
    citation_hint: str | None = None


class QueryDraftContext(Loose):
    round: Literal[1, 2, 3] = 1
    findings: list[Finding]
    requirements: list[Requirement] = Field(default_factory=list)
    hospital_name: str = "Hospital"
    tone: Literal["standard", "firm"] = "standard"
    prior_queries: list[dict[str, Any]] = Field(default_factory=list)
    claim_ref: str | None = None
    deadline_text: str | None = None  # round 3: supplied by the API from config, never invented


class TriageContext(Loose):
    open_findings: list[Finding]
    requested_doc_types: list[str] = Field(default_factory=list)
    response_text_masked: str = ""
    attached_docs: list[DocSummary] = Field(default_factory=list)
    code_check: dict[str, list[str]] = Field(default_factory=dict)


class SupervisorContext(Loose):
    step_outputs: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------------------------------
class TokenUsage(Strict):
    prompt: int = 0
    completion: int = 0
    total: int = 0


class AgentOutputBase(Strict):
    trace_id: str = ""
    prompt_version: str = ""
    model_alias: str = ""
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    degraded: bool = False
    insufficient_evidence: bool = False
    warnings: list[str] = Field(default_factory=list)


class Evidence(Strict):
    doc_id: str
    page: int | None = None
    field: str | None = None
    snippet: str | None = Field(default=None, max_length=200)


class FieldObservation(Strict):
    field: Literal["name", "dob", "gender", "policy_number", "member_id", "address", "phone_last4"]
    value_masked: str
    source: Evidence
    matches_record: Literal["match", "variation", "mismatch", "unreadable"]
    note: str | None = Field(default=None, max_length=200)


class IdentityCore(Strict):
    field_observations: list[FieldObservation] = Field(default_factory=list)
    reconciliation_notes: str = Field(default="", max_length=600)
    suspected_issue_codes: list[IdentityIssue] = Field(default_factory=list)
    insufficient_evidence: bool = False


class IdentityAgentOutput(IdentityCore, AgentOutputBase):
    pass


class Anomaly(Strict):
    code: AuthenticityIssue
    severity: Severity
    description: str = Field(max_length=300)
    evidence: list[Evidence] = Field(default_factory=list)
    source_signal: Literal["vision", "arithmetic", "duplicate", "text", "stamp"]


class AuthenticityCore(Strict):
    anomalies: list[Anomaly] = Field(default_factory=list, max_length=8)
    explanations: list[str] = Field(default_factory=list, max_length=5)
    suspected_issue_codes: list[AuthenticityIssue] = Field(default_factory=list)
    insufficient_evidence: bool = False


class AuthenticityAgentOutput(AuthenticityCore, AgentOutputBase):
    pass


class Citation(Strict):
    chunk_id: str
    doc_title: str = ""
    section: str | None = None
    quote: str = Field(max_length=400)


class ClauseHit(Strict):
    clause_ref: str
    summary: str = Field(max_length=300)
    effect: Literal["covers", "excludes", "limits", "waits", "requires"]
    citation: Citation


class CoverageCore(Strict):
    applicable_clauses: list[ClauseHit] = Field(default_factory=list)
    exclusions_hit: list[ClauseHit] = Field(default_factory=list)
    waiting_notes: str | None = Field(default=None, max_length=400)
    insufficient_evidence: bool = False


class CoverageAgentOutput(CoverageCore, AgentOutputBase):
    citations: list[Citation] = Field(default_factory=list)
    no_citation: bool = False


class MappedLine(Strict):
    line_ref: str
    mapped_group: MappedGroup
    procedure_group: str | None = None
    tags: list[str] = Field(default_factory=list)
    is_non_medical: bool = False
    is_implant: bool = False
    source: Literal["rule", "agent"] = "agent"
    rationale: str | None = Field(default=None, max_length=160)


class CalcMapCore(Strict):
    lines: list[MappedLine] = Field(default_factory=list)


class CalcMappingOutput(CalcMapCore, AgentOutputBase):
    unmapped_line_refs: list[str] = Field(default_factory=list)


class ToneCheck(Strict):
    polite: bool
    no_accusation: bool
    no_promise: bool


class QueryDraftCore(Strict):
    """What the LLM produces: one sentence per finding key (headings/closing come from the template)."""

    sentences: dict[str, str]
    citations: list[Citation] = Field(default_factory=list)


class QueryDraftOutput(AgentOutputBase):
    subject: str
    text: str = Field(max_length=1200)
    requested_doc_types: list[DocType] = Field(default_factory=list)
    finding_keys: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    tone_check: ToneCheck


class TriageCore(Strict):
    verdict: Literal["resolved", "partially_resolved", "unresolved", "off_topic"]
    resolved_finding_keys: list[str] = Field(default_factory=list)
    remaining_finding_keys: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=400)


class TriageOutput(TriageCore, AgentOutputBase):
    missing_doc_types: list[str] = Field(default_factory=list)


class Disagreement(Strict):
    topic: str
    sources: list[str] = Field(default_factory=list)
    description: str = Field(max_length=300)


class SupervisorCore(Strict):
    summary_for_reviewer: str = Field(max_length=1000)
    disagreements: list[Disagreement] = Field(default_factory=list)
    recommended_next_action: Literal["proceed", "needs_info", "manual_review", "escalate"]


class SupervisorOutput(SupervisorCore, AgentOutputBase):
    pass


ALL_OUTPUTS: dict[str, type[BaseModel]] = {
    "identity": IdentityAgentOutput, "authenticity": AuthenticityAgentOutput, "coverage": CoverageAgentOutput, "calc_mapper": CalcMappingOutput,
    "query_drafter": QueryDraftOutput, "triage": TriageOutput, "supervisor": SupervisorOutput,
}
ALL_CORES: dict[str, type[BaseModel]] = {
    "identity": IdentityCore, "authenticity": AuthenticityCore, "coverage": CoverageCore, "calc_mapper": CalcMapCore, "query_drafter": QueryDraftCore,
    "triage": TriageCore, "supervisor": SupervisorCore,
}
