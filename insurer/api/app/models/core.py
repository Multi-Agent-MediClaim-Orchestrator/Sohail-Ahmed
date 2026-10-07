"""SQLAlchemy 2 models for schema ``core`` / ``config`` / ``ops`` (mirror the Alembic migrations)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from claim_contract.enums import InsurerCaseStatus
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column

from ..ids import uuid7

TS = DateTime(timezone=True)
CaseStatus = PGEnum(*[e.value for e in InsurerCaseStatus], name="insurer_case_status", schema="core", create_type=False)
JSON = JSONB
Money = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)


def fk(target: str, *, nullable: bool = False) -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), ForeignKey(target), nullable=nullable)


# ----------------------------------------------------------------------------------------------- master data
class InsuranceProduct(Base):
    __tablename__ = "insurance_product"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    insurer_name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Policy(Base):
    __tablename__ = "policy"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    policy_number: Mapped[str] = mapped_column(Text, unique=True)
    product_id: Mapped[UUID] = fk("core.insurance_product.id")
    policy_holder_name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    sum_insured: Mapped[Decimal] = mapped_column(Money)
    cumulative_bonus: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    status: Mapped[str] = mapped_column(Text)
    premium_paid_until: Mapped[date | None] = mapped_column(Date)
    grace_days: Mapped[int] = mapped_column(Integer, default=30)


class PolicyMember(Base):
    __tablename__ = "policy_member"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    policy_id: Mapped[UUID] = fk("core.policy.id")
    member_id: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    full_name_norm: Mapped[str] = mapped_column(Text)
    dob: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(1))
    relationship: Mapped[str] = mapped_column(Text)
    id_proof_hash: Mapped[str | None] = mapped_column(Text)
    cover_start: Mapped[date] = mapped_column(Date)
    pre_existing: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class PolicyClaimUtilisation(Base):
    __tablename__ = "policy_claim_utilisation"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    policy_id: Mapped[UUID] = fk("core.policy.id")
    policy_year: Mapped[int] = mapped_column(Integer)
    utilised_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))


class NetworkHospital(Base):
    __tablename__ = "network_hospital"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    hospital_code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    rohini_id: Mapped[str | None] = mapped_column(Text)
    network_status: Mapped[str] = mapped_column(Text)
    room_rent_tier: Mapped[str | None] = mapped_column(Text)
    empanelment_valid_till: Mapped[date | None] = mapped_column(Date)
    hmac_key_id: Mapped[str] = mapped_column(Text, unique=True)
    callback_base_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    account_hash: Mapped[str | None] = mapped_column(Text)
    watchlist: Mapped[bool] = mapped_column(Boolean, default=False)


# ----------------------------------------------------------------------------------------------- claims
class ClaimCase(Base):
    __tablename__ = "claim_case"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    insurer_claim_no: Mapped[str] = mapped_column(Text, unique=True)
    hospital_claim_ref: Mapped[str] = mapped_column(Text)
    hospital_id: Mapped[UUID] = fk("core.network_hospital.id")
    claim_type: Mapped[str] = mapped_column(Text)
    admission_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(CaseStatus, default="received")
    policy_id: Mapped[UUID | None] = fk("core.policy.id", nullable=True)
    member_id: Mapped[UUID | None] = fk("core.policy_member.id", nullable=True)
    claimed_amount: Mapped[Decimal] = mapped_column(Money)
    recommended_amount: Mapped[Decimal | None] = mapped_column(Money)
    approved_amount: Mapped[Decimal | None] = mapped_column(Money)
    contract_version: Mapped[str] = mapped_column(Text)
    submission: Mapped[dict[str, Any]] = deferred(mapped_column(JSON))
    submission_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(TS)
    assigned_reviewer: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)
    sla_due_at: Mapped[datetime | None] = mapped_column(TS)
    closure_reason: Mapped[str | None] = mapped_column(Text)
    last_callback_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    latest_run_id: Mapped[UUID | None] = fk("core.verification_run.id", nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False)
    procedure_group: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    journey_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pending_rerun: Mapped[bool] = mapped_column(Boolean, default=False)
    last_inbound_seq: Mapped[int] = mapped_column(BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class ClaimDocument(Base):
    __tablename__ = "claim_document"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)  # = hospital doc_id
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    doc_type: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    pages: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str | None] = mapped_column(Text)
    fetch_status: Mapped[str] = mapped_column(Text, default="pending")
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    parse_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    superseded_by: Mapped[UUID | None] = fk("core.claim_document.id", nullable=True)
    added_via_query: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_url_enc: Mapped[str | None] = mapped_column(Text)
    fetch_error: Mapped[str | None] = mapped_column(Text)
    refresh_attempts: Mapped[int] = mapped_column(Integer, default=0)
    scan_result: Mapped[str | None] = mapped_column(Text)
    vision: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extract: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class BillLine(Base):
    __tablename__ = "bill_line"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    line_no: Mapped[int] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)
    service_date: Mapped[date | None] = mapped_column(Date)
    source_doc_id: Mapped[UUID | None] = fk("core.claim_document.id", nullable=True)
    source_page: Mapped[int | None] = mapped_column(Integer)


# ----------------------------------------------------------------------------------------------- verification
class VerificationRun(Base):
    __tablename__ = "verification_run"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    run_no: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")
    started_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    config_versions: Mapped[dict[str, Any]] = mapped_column(JSON)
    client_token: Mapped[str | None] = mapped_column(Text)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(Text)


class VerificationStep(Base):
    __tablename__ = "verification_step"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    run_id: Mapped[UUID] = fk("core.verification_run.id")
    step: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    agent_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    deterministic: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class CalculationResult(Base):
    __tablename__ = "calculation_result"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    run_id: Mapped[UUID | None] = fk("core.verification_run.id", nullable=True)
    engine_version: Mapped[str] = mapped_column(Text)
    policy_rules_version: Mapped[int] = mapped_column(Integer)
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any]] = mapped_column(JSON)
    payable_amount: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class FindingOverride(Base):
    __tablename__ = "finding_override"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    run_id: Mapped[UUID] = fk("core.verification_run.id")
    finding_key: Mapped[str] = mapped_column(Text)
    finding_code: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    overridden_by: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class AgentRun(Base):
    __tablename__ = "agent_run"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    agent: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model_alias: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


# ----------------------------------------------------------------------------------------------- queries
class Query(Base):
    __tablename__ = "query"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    round: Mapped[int] = mapped_column(SmallInteger)
    category: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    requested_doc_types: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    status: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    draft_text: Mapped[str | None] = mapped_column(Text)
    draft_citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    raised_by: Mapped[str | None] = mapped_column(Text)
    due_by: Mapped[datetime] = mapped_column(TS)
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    answered_at: Mapped[datetime | None] = mapped_column(TS)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    triage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    callback_seq: Mapped[int | None] = mapped_column(BigInteger)
    dedupe_key: Mapped[str | None] = mapped_column(String(64))
    reminder_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    finding_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_source: Mapped[str | None] = mapped_column(Text)
    lint_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    acked_at: Mapped[datetime | None] = mapped_column(TS)
    is_extension: Mapped[bool] = mapped_column(Boolean, default=False)
    regen_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    closed_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class QueryRound(Base):
    __tablename__ = "query_round"
    __table_args__ = {"schema": "core"}
    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("core.claim_case.id"), primary_key=True)
    round: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    opened_at: Mapped[datetime] = mapped_column(TS)
    due_by: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    outcome: Mapped[str | None] = mapped_column(Text)


class QueryResponse(Base):
    __tablename__ = "query_response"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    query_id: Mapped[UUID] = fk("core.query.id")
    answer_text: Mapped[str] = mapped_column(Text)
    attached_doc_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), default=list)
    responded_by: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    triage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    triage_source: Mapped[str | None] = mapped_column(Text)
    amends: Mapped[UUID | None] = fk("core.query_response.id", nullable=True)


class Escalation(Base):
    __tablename__ = "escalation"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    pack: Mapped[dict[str, Any]] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


# ----------------------------------------------------------------------------------------------- decisions
class Decision(Base):
    __tablename__ = "decision"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    kind: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    approved_amount: Mapped[Decimal | None] = mapped_column(Money)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    deductions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    explanation: Mapped[str | None] = mapped_column(Text)
    calc_result_id: Mapped[UUID | None] = fk("core.calculation_result.id", nullable=True)
    gate_tier: Mapped[str | None] = mapped_column(Text)
    config_versions: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    supersedes: Mapped[UUID | None] = fk("core.decision.id", nullable=True)
    status: Mapped[str] = mapped_column(Text, default="proposed")
    revision: Mapped[int] = mapped_column(SmallInteger, default=1)
    override_reason: Mapped[str | None] = mapped_column(Text)
    gate_amount: Mapped[Decimal | None] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    finalised_at: Mapped[datetime | None] = mapped_column(TS)
    reviewer_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class DecisionTask(Base):
    __tablename__ = "decision_task"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    decision_id: Mapped[UUID] = fk("core.decision.id")
    tier: Mapped[str] = mapped_column(Text)
    required_approvals: Mapped[int] = mapped_column(SmallInteger)
    min_senior: Mapped[int] = mapped_column(SmallInteger, default=0)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(Text)
    threshold_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    opened_by: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    close_reason: Mapped[str | None] = mapped_column(Text)


class Approval(Base):
    __tablename__ = "approval"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    decision_id: Mapped[UUID] = fk("core.decision.id")
    approver: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    approver_role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    task_id: Mapped[UUID | None] = fk("core.decision_task.id", nullable=True)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


# ----------------------------------------------------------------------------------------------- settlement
class Settlement(Base):
    __tablename__ = "settlement"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = fk("core.claim_case.id")
    amount: Mapped[Decimal] = mapped_column(Money)
    mode: Mapped[str] = mapped_column(Text, default="neft_sim")
    utr: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    initiated_at: Mapped[datetime | None] = mapped_column(TS)
    paid_at: Mapped[datetime | None] = mapped_column(TS)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    payee_type: Mapped[str] = mapped_column(Text, default="hospital")
    payee_ref: Mapped[str] = mapped_column(Text, default="")
    beneficiary_account_hash: Mapped[str | None] = mapped_column(String(64))
    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    adjustments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid7)
    next_retry_at: Mapped[datetime | None] = mapped_column(TS)
    release_utilisation: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class SettlementEvent(Base):
    __tablename__ = "settlement_event"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    settlement_id: Mapped[UUID] = fk("core.settlement.id")
    event: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class SettlementTask(Base):
    __tablename__ = "settlement_task"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    settlement_id: Mapped[UUID | None] = fk("core.settlement.id", nullable=True)
    case_id: Mapped[UUID | None] = fk("core.claim_case.id", nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    closed_by: Mapped[str | None] = mapped_column(Text)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_run"
    __table_args__ = {"schema": "core"}
    id: Mapped[UUID] = pk()
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    diffs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


# ----------------------------------------------------------------------------------------------- config / ops
class ConfigSet(Base):
    __tablename__ = "config_set"
    __table_args__ = {"schema": "config"}
    id: Mapped[UUID] = pk()
    domain: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")


class ConfigVersion(Base):
    __tablename__ = "config_version"
    __table_args__ = {"schema": "config"}
    id: Mapped[UUID] = pk()
    config_set_id: Mapped[UUID] = fk("config.config_set.id")
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="draft")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_schema: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[datetime | None] = mapped_column(TS)
    effective_to: Mapped[datetime | None] = mapped_column(TS)
    change_note: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    published_by: Mapped[str | None] = mapped_column(Text)
    second_approver: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TS)


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": "ops"}
    id: Mapped[UUID] = pk()
    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    endpoint: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    seq: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default="now()")
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    kind: Mapped[str] = mapped_column(Text, default="callback")
    base_url: Mapped[str | None] = mapped_column(Text)


class UserProfile(Base):
    __tablename__ = "user_profile"
    __table_args__ = {"schema": "ops"}
    sub: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    skill_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    on_leave: Mapped[bool] = mapped_column(Boolean, default=False)
    last_assigned_at: Mapped[datetime | None] = mapped_column(TS)
