"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from claim_contract.enums import (
    DocType,
)
from sqlalchemy import (
    ARRAY,
    CHAR,
    REAL,
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import INT4RANGE, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "(classification_confidence IS NULL OR classification_confidence >= 0::double precision AND classification_confidence <= 1::double precision) AND (parse_confidence IS NULL OR parse_confidence >= 0::double precision AND parse_confidence <= 1::double precision) AND (quality_score IS NULL OR quality_score >= 0::double precision AND quality_score <= 1::double precision)",
            name="ck_document_conf",
        ),
        CheckConstraint(
            "doc_type_source IS NULL OR (doc_type_source = ANY (ARRAY['auto'::text, 'manual'::text]))",
            name="ck_document_type_source",
        ),
        CheckConstraint(
            "lifecycle = ANY (ARRAY['active'::text, 'superseded'::text, 'deleted'::text, 'quarantined'::text])",
            name="ck_document_lifecycle",
        ),
        CheckConstraint(
            "parse_status = ANY (ARRAY['pending'::text, 'processing'::text, 'parsed'::text, 'failed'::text, 'needs_review'::text])",
            name="ck_document_parse_status",
        ),
        CheckConstraint(
            "scan_status <> 'infected'::text OR lifecycle = 'quarantined'::text",
            name="ck_document_infected_lifecycle",
        ),
        CheckConstraint(
            "scan_status = ANY (ARRAY['pending'::text, 'clean'::text, 'infected'::text, 'error'::text])",
            name="ck_document_scan_status",
        ),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 26214400", name="ck_document_size"),
        CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id", name="ck_document_no_self_ref"
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="document_case_id_fkey"),
        ForeignKeyConstraint(["deleted_by"], ["app_user.id"], name="document_deleted_by_fkey"),
        ForeignKeyConstraint(["parent_id"], ["document.id"], name="document_parent_id_fkey"),
        ForeignKeyConstraint(
            ["supersedes_id"], ["document.id"], name="document_supersedes_id_fkey"
        ),
        ForeignKeyConstraint(["uploaded_by"], ["app_user.id"], name="document_uploaded_by_fkey"),
        PrimaryKeyConstraint("id", name="document_pkey"),
        UniqueConstraint("case_id", "sha256", name="uq_document_case_sha"),
        Index("ix_doc_case", "case_id", postgresql_where="(lifecycle = 'active'::text)"),
        Index("ix_doc_case_all", "case_id"),
        Index("ix_doc_parent", "parent_id", postgresql_where="(parent_id IS NOT NULL)"),
        Index(
            "ix_doc_pending_parse",
            "last_trigger_at",
            postgresql_where="((parse_status = 'pending'::text) AND (scan_status = 'clean'::text))",
        ),
        Index("ix_doc_purge", "purge_after", postgresql_where="(purge_after IS NOT NULL)"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    scan_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'::text")
    )
    lifecycle: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'::text")
    )
    parse_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'::text")
    )
    parse_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    quality_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    pages: Mapped[int | None] = mapped_column(Integer)
    doc_type: Mapped[DocType | None] = mapped_column(
        Enum(DocType, values_callable=lambda cls: [member.value for member in cls], name="doc_type")
    )
    doc_type_source: Mapped[str | None] = mapped_column(Text)
    classification_confidence: Mapped[float | None] = mapped_column(REAL)
    last_trigger_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    parse_confidence: Mapped[float | None] = mapped_column(REAL)
    quality_score: Mapped[float | None] = mapped_column(REAL)
    has_required_stamp: Mapped[bool | None] = mapped_column(Boolean)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    page_range: Mapped[Any | None] = mapped_column(INT4RANGE)
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    purge_after: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class DocumentParse(Base):
    __tablename__ = "document_parse"
    __table_args__ = (
        CheckConstraint("pass_no >= 1 AND pass_no <= 3", name="ck_document_parse_pass"),
        CheckConstraint(
            "typed_json IS NULL OR octet_length(typed_json::text) <= 1048576",
            name="ck_document_parse_size",
        ),
        ForeignKeyConstraint(
            ["document_id"], ["document.id"], name="document_parse_document_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="document_parse_pkey"),
        UniqueConstraint("document_id", "pass_no", name="uq_document_parse_pass"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    pass_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    engine: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    engine_version: Mapped[str | None] = mapped_column(Text)
    raw_markdown_key: Mapped[str | None] = mapped_column(Text)
    masked_text_key: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[Any | None] = mapped_column(JSONB)
    typed_json: Mapped[Any | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(REAL)
    agreement_score: Mapped[float | None] = mapped_column(REAL)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class DocRequest(Base):
    __tablename__ = "doc_request"
    __table_args__ = (
        CheckConstraint(
            "status <> 'waived'::text OR waived_by IS NOT NULL AND waive_reason IS NOT NULL",
            name="ck_doc_request_waive",
        ),
        CheckConstraint(
            "status = ANY (ARRAY['open'::text, 'fulfilled'::text, 'waived'::text, 'expired'::text])",
            name="ck_doc_request_status",
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="doc_request_case_id_fkey"),
        ForeignKeyConstraint(
            ["fulfilled_by_doc"], ["document.id"], name="doc_request_fulfilled_by_doc_fkey"
        ),
        ForeignKeyConstraint(["waived_by"], ["app_user.id"], name="doc_request_waived_by_fkey"),
        PrimaryKeyConstraint("id", name="doc_request_pkey"),
        Index("ix_doc_request_due", "due_by", postgresql_where="(status = 'open'::text)"),
        Index(
            "uq_doc_request_open",
            "case_id",
            "doc_type",
            postgresql_where="(status = 'open'::text)",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    doc_type: Mapped[DocType] = mapped_column(
        Enum(
            DocType, values_callable=lambda cls: [member.value for member in cls], name="doc_type"
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'::text"))
    reminders_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    rule_id: Mapped[str | None] = mapped_column(Text)
    due_by: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    fulfilled_by_doc: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    waived_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    waive_reason: Mapped[str | None] = mapped_column(Text)
