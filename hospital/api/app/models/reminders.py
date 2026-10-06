"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
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


class Reminder(Base):
    __tablename__ = "reminder"
    __table_args__ = (
        CheckConstraint(
            "channel = ANY (ARRAY['in_app'::text, 'email'::text])", name="ck_reminder_channel"
        ),
        CheckConstraint(
            "kind = ANY (ARRAY['doc_request'::text, 'query_sla'::text, 'filing_deadline'::text, 'intimation'::text, 'submission_nudge'::text])",
            name="ck_reminder_kind",
        ),
        CheckConstraint(
            "status = ANY (ARRAY['scheduled'::text, 'fired'::text, 'cancelled'::text, 'failed'::text])",
            name="ck_reminder_status",
        ),
        ForeignKeyConstraint(["case_id"], ["claim_case.id"], name="reminder_case_id_fkey"),
        PrimaryKeyConstraint("id", name="reminder_pkey"),
        UniqueConstraint("case_id", "kind", "ref_id", "fire_at", name="uq_reminder_dedup"),
        Index("ix_reminder_due", "fire_at", postgresql_where="(status = 'scheduled'::text)"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'in_app'::text")
    )
    fire_at: Mapped[datetime.datetime] = mapped_column(DateTime(True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'scheduled'::text")
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    fired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[Any | None] = mapped_column(JSONB)
