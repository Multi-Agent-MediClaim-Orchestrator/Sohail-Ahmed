"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import enum
import uuid
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
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


class ConfigStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ConfigSet(Base):
    __tablename__ = "config_set"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="config_set_pkey"),
        UniqueConstraint("domain", "name", name="uq_config_set"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    description: Mapped[str | None] = mapped_column(Text)


class ConfigVersion(Base):
    __tablename__ = "config_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="ck_config_version_window"
        ),
        CheckConstraint(
            "second_approver IS NULL OR second_approver <> published_by",
            name="ck_config_publish_distinct",
        ),
        CheckConstraint(
            "status <> 'published'::config_status OR effective_from IS NOT NULL AND published_by IS NOT NULL",
            name="ck_config_version_published",
        ),
        ForeignKeyConstraint(
            ["config_set_id"], ["config_set.id"], name="config_version_config_set_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="config_version_pkey"),
        UniqueConstraint("config_set_id", "version", name="uq_config_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    config_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ConfigStatus] = mapped_column(
        Enum(
            ConfigStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="config_status",
        ),
        nullable=False,
        server_default=text("'draft'::config_status"),
    )
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    payload_schema: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    effective_from: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    effective_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    published_by: Mapped[str | None] = mapped_column(Text)
    second_approver: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
