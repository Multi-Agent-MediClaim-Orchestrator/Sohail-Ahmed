"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from claim_contract.enums import (
    DocType,
    QueryCategory,
    QueryStatus,
)
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class InsurerQuery(Base):
    __tablename__ = "insurer_query"
    __table_args__ = (
        CheckConstraint("round >= 1 AND round <= 3", name="ck_insurer_query_round"),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="insurer_query_case_id_fkey"),
        PrimaryKeyConstraint("id", name="insurer_query_pkey"),
        UniqueConstraint("insurer_query_id", name="uq_insurer_query_id"),
        Index("ix_query_case", "case_id", "round"),
        Index(
            "ix_query_open_due",
            "due_by",
            postgresql_where="(status = ANY (ARRAY['open'::query_status, 'draft_ready'::query_status]))",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    insurer_query_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    round: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category: Mapped[QueryCategory] = mapped_column(
        Enum(
            QueryCategory,
            values_callable=lambda cls: [member.value for member in cls],
            name="query_category",
        ),
        nullable=False,
    )
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    requested_doc_types: Mapped[list[DocType]] = mapped_column(
        ARRAY(
            Enum(
                DocType,
                values_callable=lambda cls: [member.value for member in cls],
                name="doc_type",
            )
        ),
        nullable=False,
        server_default=text("'{}'::doc_type[]"),
    )
    status: Mapped[QueryStatus] = mapped_column(
        Enum(
            QueryStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="query_status",
        ),
        nullable=False,
        server_default=text("'open'::query_status"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    due_by: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    triage: Mapped[Any | None] = mapped_column(JSONB)
    overdue_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    responded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))


class QueryResponse(Base):
    __tablename__ = "query_response"
    __table_args__ = (
        CheckConstraint(
            "second_approver IS NULL OR second_approver <> approved_by",
            name="ck_query_response_distinct",
        ),
        CheckConstraint(
            "source = ANY (ARRAY['agent'::text, 'human_edit'::text])",
            name="ck_query_response_source",
        ),
        CheckConstraint(
            "status = ANY (ARRAY['draft'::text, 'approved'::text, 'sent'::text])",
            name="ck_query_response_status",
        ),
        ForeignKeyConstraint(
            ["approved_by"], ["app_user.id"], name="query_response_approved_by_fkey"
        ),
        ForeignKeyConstraint(
            ["query_id"], ["insurer_query.id"], name="query_response_query_id_fkey"
        ),
        ForeignKeyConstraint(
            ["second_approver"], ["app_user.id"], name="query_response_second_approver_fkey"
        ),
        PrimaryKeyConstraint("id", name="query_response_pkey"),
        UniqueConstraint("query_id", "version", name="uq_query_response_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    query_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    attached_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid()), nullable=False, server_default=text("'{}'::uuid[]")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'::text"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    citations: Mapped[Any | None] = mapped_column(JSONB)
    unsupported_claims: Mapped[Any | None] = mapped_column(JSONB)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    second_approver: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
