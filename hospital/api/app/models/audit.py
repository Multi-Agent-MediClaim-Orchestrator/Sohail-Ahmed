"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from claim_contract.enums import ActorType as AuditActorType
from sqlalchemy import (
    CHAR,
    BigInteger,
    Date,
    DateTime,
    Enum,
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


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="audit_event_pkey"),
        UniqueConstraint("case_id", "seq", name="uq_audit_case_seq"),
        Index("ix_audit_case_ts", "case_id", "seq"),
        Index("ix_audit_type", "event_type", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(True), nullable=False)
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(
            AuditActorType,
            values_callable=lambda cls: [member.value for member in cls],
            name="audit_actor_type",
        ),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    config_versions: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    model_info: Mapped[Any | None] = mapped_column(JSONB)
    journey_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class CaseAuditHead(Base):
    __tablename__ = "case_audit_head"
    __table_args__ = (PrimaryKeyConstraint("case_id", name="case_audit_head_pkey"),)

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )


class AuditAnchor(Base):
    __tablename__ = "audit_anchor"
    __table_args__ = (PrimaryKeyConstraint("anchor_date", name="audit_anchor_pkey"),)

    anchor_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    merkle_root: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    object_key: Mapped[str | None] = mapped_column(Text)
