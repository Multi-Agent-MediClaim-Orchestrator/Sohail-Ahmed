"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "method = ANY (ARRAY['POST'::text, 'PUT'::text, 'GET'::text])", name="ck_outbox_method"
        ),
        CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'failed'::text, 'dead'::text])",
            name="ck_outbox_status",
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="outbox_case_id_fkey"),
        PrimaryKeyConstraint("id", name="outbox_pkey"),
        UniqueConstraint("idempotency_key", name="uq_outbox_idem"),
        Index("ix_outbox_case", "case_id", "sequence"),
        Index("ix_outbox_dead", "created_at", postgresql_where="(status = 'dead'::text)"),
        Index(
            "ix_outbox_due",
            "next_attempt_at",
            postgresql_where="(status = ANY (ARRAY['pending'::text, 'sending'::text]))",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[Any] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'::text")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    body_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[Any | None] = mapped_column(JSONB)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))


class InboundCallback(Base):
    __tablename__ = "inbound_callback"
    __table_args__ = (
        CheckConstraint(
            "kind = ANY (ARRAY['status'::text, 'queries'::text, 'decisions'::text, 'settlements'::text])",
            name="ck_inbound_kind",
        ),
        PrimaryKeyConstraint("id", name="inbound_callback_pkey"),
        UniqueConstraint("claim_ref", "kind", "sequence", name="uq_inbound_seq"),
        UniqueConstraint("idempotency_key", name="uq_inbound_idem"),
        Index("ix_inbound_unprocessed", "received_at", postgresql_where="(processed = false)"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    claim_ref: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    body: Mapped[Any] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    response_status: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("204")
    )
    process_error: Mapped[str | None] = mapped_column(Text)
    response_body: Mapped[Any | None] = mapped_column(JSONB)


class RouteHistory(Base):
    __tablename__ = "route_history"
    __table_args__ = (
        CheckConstraint(
            "trigger = ANY (ARRAY['create'::text, 'patch'::text, 'doc_classified'::text, 'draft_totals'::text, 'config_republish'::text, 'manual'::text, 'override'::text, 'ack'::text, 'post_submission'::text])",
            name="ck_route_history_trigger",
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="route_history_case_id_fkey"),
        PrimaryKeyConstraint("id", name="route_history_pkey"),
        UniqueConstraint("case_id", "seq", name="uq_route_history_seq"),
        Index("ix_route_history_case", "case_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[Any] = mapped_column(JSONB, nullable=False)
    decision_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
