"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import decimal
import uuid
from typing import Any

from claim_contract.enums import (
    DocType,
)
from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CompletenessCheck(Base):
    __tablename__ = "completeness_check"
    __table_args__ = (
        CheckConstraint(
            "trigger = ANY (ARRAY['doc_event'::text, 'manual'::text, 'waive'::text, 'reclassify'::text, 'nightly'::text, 'config_republish'::text, 'claim_build_precheck'::text])",
            name="ck_completeness_trigger",
        ),
        ForeignKeyConstraint(
            ["case_id"], ["claim_case.id"], name="completeness_check_case_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="completeness_check_pkey"),
        UniqueConstraint("case_id", "run_no", name="uq_completeness_run"),
        Index("ix_completeness_case_latest", "case_id", "run_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_no: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result: Mapped[Any] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    result_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)


class RequirementWaiver(Base):
    __tablename__ = "requirement_waiver"
    __table_args__ = (
        CheckConstraint("length(reason) >= 10", name="ck_requirement_waiver_reason"),
        ForeignKeyConstraint(
            ["case_id"], ["claim_case.id"], name="requirement_waiver_case_id_fkey"
        ),
        ForeignKeyConstraint(
            ["revoked_by"], ["app_user.id"], name="requirement_waiver_revoked_by_fkey"
        ),
        ForeignKeyConstraint(
            ["waived_by"], ["app_user.id"], name="requirement_waiver_waived_by_fkey"
        ),
        PrimaryKeyConstraint("id", name="requirement_waiver_pkey"),
        Index(
            "uq_requirement_waiver_active",
            "case_id",
            "rule_id",
            postgresql_where="(revoked_at IS NULL)",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[DocType] = mapped_column(
        Enum(
            DocType, values_callable=lambda cls: [member.value for member in cls], name="doc_type"
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    waived_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class ClaimDraft(Base):
    __tablename__ = "claim_draft"
    __table_args__ = (
        CheckConstraint(
            "source = ANY (ARRAY['agent'::text, 'human_edit'::text, 'repair'::text])",
            name="ck_claim_draft_source",
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="claim_draft_case_id_fkey"),
        PrimaryKeyConstraint("id", name="claim_draft_pkey"),
        UniqueConstraint("case_id", "version", name="uq_claim_draft_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    validation: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    provenance: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    has_errors: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    model_info: Mapped[Any | None] = mapped_column(JSONB)
    edit_summary: Mapped[Any | None] = mapped_column(JSONB)
    estimate: Mapped[Any | None] = mapped_column(JSONB)


class BillLine(Base):
    __tablename__ = "bill_line"
    __table_args__ = (
        CheckConstraint(
            "category = ANY (ARRAY['room'::text, 'icu'::text, 'surgery'::text, 'anaesthesia'::text, 'medicine'::text, 'consumable'::text, 'implant'::text, 'investigation'::text, 'consultation'::text, 'other'::text])",
            name="ck_bill_line_category",
        ),
        CheckConstraint(
            "qty >= 0::numeric AND unit_price >= 0::numeric AND amount >= 0::numeric",
            name="ck_bill_line_nonneg",
        ),
        ForeignKeyConstraint(
            ["draft_id"], ["claim_draft.id"], ondelete="CASCADE", name="bill_line_draft_id_fkey"
        ),
        ForeignKeyConstraint(
            ["source_document_id"], ["document.id"], name="bill_line_source_document_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="bill_line_pkey"),
        UniqueConstraint("draft_id", "line_no", name="uq_bill_line_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    unit_price: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    code: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_page: Mapped[int | None] = mapped_column(Integer)


class Signoff(Base):
    __tablename__ = "signoff"
    __table_args__ = (
        CheckConstraint(
            "decision = ANY (ARRAY['approved'::text, 'returned'::text])", name="ck_signoff_decision"
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="signoff_case_id_fkey"),
        ForeignKeyConstraint(["draft_id"], ["claim_draft.id"], name="signoff_draft_id_fkey"),
        ForeignKeyConstraint(["officer_id"], ["app_user.id"], name="signoff_officer_id_fkey"),
        PrimaryKeyConstraint("id", name="signoff_pkey"),
        UniqueConstraint("draft_id", "officer_id", name="uq_signoff_officer_draft"),
        Index(
            "ux_signoff_active",
            "draft_id",
            postgresql_where="((decision = 'approved'::text) AND (invalidated_at IS NULL))",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    officer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    acknowledged_warnings: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    comment: Mapped[str | None] = mapped_column(Text)
    invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    invalidated_reason: Mapped[str | None] = mapped_column(Text)


class Settlement(Base):
    __tablename__ = "settlement"
    __table_args__ = (
        CheckConstraint("amount >= 0::numeric AND tds >= 0::numeric", name="ck_settlement_amount"),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="settlement_case_id_fkey"),
        PrimaryKeyConstraint("id", name="settlement_pkey"),
        UniqueConstraint("case_id", "utr", name="uq_settlement_case_utr"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    settlement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    utr: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tds: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    paid_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    raw: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
