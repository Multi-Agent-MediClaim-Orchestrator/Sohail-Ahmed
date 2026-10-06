"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            "role = ANY (ARRAY['desk'::text, 'officer'::text, 'admin'::text])",
            name="ck_app_user_role",
        ),
        PrimaryKeyConstraint("id", name="app_user_pkey"),
        UniqueConstraint("keycloak_sub", name="uq_app_user_keycloak_sub"),
        Index("ix_app_user_email", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    keycloak_sub: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))


class Hospital(Base):
    __tablename__ = "hospital"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="hospital_pkey"),
        UniqueConstraint("code", name="uq_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    nabh_accredited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    hmac_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    rohini_id: Mapped[str | None] = mapped_column(Text)
    address: Mapped[Any | None] = mapped_column(JSONB)


class Patient(Base):
    __tablename__ = "patient"
    __table_args__ = (
        CheckConstraint("dob <= CURRENT_DATE", name="ck_patient_dob"),
        CheckConstraint(
            "gender = ANY (ARRAY['M'::bpchar, 'F'::bpchar, 'O'::bpchar])", name="ck_patient_gender"
        ),
        PrimaryKeyConstraint("id", name="patient_pkey"),
        UniqueConstraint("uhid", name="uq_patient_uhid"),
        Index(
            "ix_patient_name_trgm",
            "full_name",
            postgresql_ops={"full_name": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
        Index(
            "ix_patient_uhid_trgm",
            "uhid",
            postgresql_ops={"uhid": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    uhid: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    dob: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    gender: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    id_proof_type: Mapped[str | None] = mapped_column(Text)
    id_proof_hash: Mapped[str | None] = mapped_column(CHAR(64))
    id_proof_last4: Mapped[str | None] = mapped_column(CHAR(4))


class InsurancePolicyRef(Base):
    __tablename__ = "insurance_policy_ref"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_policy_ref_validity",
        ),
        ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="insurance_policy_ref_patient_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="insurance_policy_ref_pkey"),
        UniqueConstraint("patient_id", "policy_number", "member_id", name="uq_policy_ref"),
        Index("ix_policy_ref_patient", "patient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    insurer_name: Mapped[str] = mapped_column(Text, nullable=False)
    policy_number: Mapped[str] = mapped_column(Text, nullable=False)
    member_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    valid_from: Mapped[datetime.date | None] = mapped_column(Date)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date)
