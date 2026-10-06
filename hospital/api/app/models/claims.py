"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
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
        ForeignKeyConstraint(
            ["case_id"], ["claim_case.id"], name="completeness_check_case_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="completeness_check_pkey"),
        UniqueConstraint("case_id", "run_no", name="uq_completeness_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_no: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    trigger: Mapped[str | None] = mapped_column(Text)


class ClaimDraft(Base):
    __tablename__ = "claim_draft"
    __table_args__ = (
        CheckConstraint(
            "source = ANY (ARRAY['agent'::text, 'human_edit'::text])", name="ck_claim_draft_source"
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="claim_draft_case_id_fkey"),
        PrimaryKeyConstraint("id", name="claim_draft_pkey"),
        UniqueConstraint("case_id", "version", name="uq_claim_draft_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    validation: Mapped[Any | None] = mapped_column(JSONB)


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
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    officer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    comment: Mapped[str | None] = mapped_column(Text)
