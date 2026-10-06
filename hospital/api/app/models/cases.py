"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import decimal
import uuid
from typing import Any

from claim_contract.enums import (
    AdmissionType,
    ClaimType,
    HospitalCaseStatus,
)
from sqlalchemy import (
    ARRAY,
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


class ClaimCase(Base):
    __tablename__ = "claim_case"
    __table_args__ = (
        CheckConstraint(
            "(claimed_amount IS NULL OR claimed_amount >= 0::numeric) AND (preauth_amount IS NULL OR preauth_amount >= 0::numeric) AND (approved_amount IS NULL OR approved_amount >= 0::numeric)",
            name="ck_case_amounts",
        ),
        CheckConstraint(
            "admission_source IS NULL OR (admission_source = ANY (ARRAY['ER'::text, 'OPD'::text, 'referral'::text]))",
            name="ck_case_admission_source",
        ),
        CheckConstraint(
            "discharged_on IS NULL OR admitted_on IS NULL OR discharged_on >= admitted_on",
            name="ck_case_dates",
        ),
        CheckConstraint(
            "preauth_amount IS NULL OR claim_type = 'cashless'::claim_type",
            name="ck_case_cashless_preauth_amount",
        ),
        ForeignKeyConstraint(["assigned_to"], ["app_user.id"], name="claim_case_assigned_to_fkey"),
        ForeignKeyConstraint(
            ["converted_from"], ["claim_case.id"], name="claim_case_converted_from_fkey"
        ),
        ForeignKeyConstraint(
            ["converted_to"], ["claim_case.id"], name="claim_case_converted_to_fkey"
        ),
        ForeignKeyConstraint(["created_by"], ["app_user.id"], name="claim_case_created_by_fkey"),
        ForeignKeyConstraint(["hospital_id"], ["hospital.id"], name="claim_case_hospital_id_fkey"),
        ForeignKeyConstraint(["patient_id"], ["patient.id"], name="claim_case_patient_id_fkey"),
        ForeignKeyConstraint(
            ["policy_ref_id"], ["insurance_policy_ref.id"], name="claim_case_policy_ref_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="claim_case_pkey"),
        UniqueConstraint("claim_ref", name="uq_claim_case_claim_ref"),
        Index("ix_case_assigned", "assigned_to", postgresql_where="(assigned_to IS NOT NULL)"),
        Index(
            "ix_case_claim_ref_trgm",
            "claim_ref",
            postgresql_ops={"claim_ref": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
        Index(
            "ix_case_converted_from",
            "converted_from",
            postgresql_where="(converted_from IS NOT NULL)",
        ),
        Index("ix_case_created_keyset", "created_at", "id"),
        Index(
            "ix_case_deadline",
            "filing_deadline",
            postgresql_where="(status <> ALL (ARRAY['closed'::hospital_case_status, 'settled'::hospital_case_status]))",
        ),
        Index("ix_case_flags", "flags", postgresql_using="gin"),
        Index(
            "ix_case_insurer_claim_no",
            "insurer_claim_no",
            postgresql_where="(insurer_claim_no IS NOT NULL)",
        ),
        Index(
            "ix_case_intimation",
            "intimation_deadline",
            postgresql_where="((intimation_deadline IS NOT NULL) AND (status = ANY (ARRAY['draft'::hospital_case_status, 'docs_pending'::hospital_case_status, 'docs_complete'::hospital_case_status, 'building_claim'::hospital_case_status, 'ready_for_review'::hospital_case_status])))",
        ),
        Index("ix_case_patient", "patient_id"),
        Index("ix_case_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    claim_ref: Mapped[str] = mapped_column(Text, nullable=False)
    hospital_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    policy_ref_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    claim_type: Mapped[ClaimType] = mapped_column(
        Enum(
            ClaimType,
            values_callable=lambda cls: [member.value for member in cls],
            name="claim_type",
        ),
        nullable=False,
    )
    admission_type: Mapped[AdmissionType] = mapped_column(
        Enum(
            AdmissionType,
            values_callable=lambda cls: [member.value for member in cls],
            name="admission_type",
        ),
        nullable=False,
    )
    status: Mapped[HospitalCaseStatus] = mapped_column(
        Enum(
            HospitalCaseStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="hospital_case_status",
        ),
        nullable=False,
        server_default=text("'draft'::hospital_case_status"),
    )
    diagnosis_codes: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    procedure_codes: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    admitted_on: Mapped[datetime.date | None] = mapped_column(Date)
    discharged_on: Mapped[datetime.date | None] = mapped_column(Date)
    procedure_group: Mapped[str | None] = mapped_column(Text)
    treating_doctor: Mapped[str | None] = mapped_column(Text)
    preauth_ref: Mapped[str | None] = mapped_column(Text)
    preauth_amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2))
    claimed_amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2))
    insurer_claim_no: Mapped[str | None] = mapped_column(Text)
    filing_deadline: Mapped[datetime.date | None] = mapped_column(Date)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    config_versions: Mapped[Any | None] = mapped_column(JSONB)
    stale_flagged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    admitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    route: Mapped[Any | None] = mapped_column(JSONB)
    intimation_deadline: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    decision: Mapped[Any | None] = mapped_column(JSONB)
    approved_amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2))
    short_pay_amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2))
    settled_amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2))
    settled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    discharged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))
    admission_source: Mapped[str | None] = mapped_column(Text)
    converted_from: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    converted_to: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    insurer_status: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(True))


class CaseStatusHistory(Base):
    __tablename__ = "case_status_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["case_id"], ["claim_case.id"], name="case_status_history_case_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="case_status_history_pkey"),
        Index("ix_case_status_history_case", "case_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    to_status: Mapped[HospitalCaseStatus] = mapped_column(
        Enum(
            HospitalCaseStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="hospital_case_status",
        ),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(True), nullable=False, server_default=text("now()")
    )
    from_status: Mapped[HospitalCaseStatus | None] = mapped_column(
        Enum(
            HospitalCaseStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="hospital_case_status",
        )
    )
    reason: Mapped[str | None] = mapped_column(Text)


class AllowedTransition(Base):
    __tablename__ = "allowed_transition"
    __table_args__ = (
        PrimaryKeyConstraint("from_status", "to_status", name="allowed_transition_pkey"),
    )

    from_status: Mapped[HospitalCaseStatus] = mapped_column(
        Enum(
            HospitalCaseStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="hospital_case_status",
        ),
        primary_key=True,
    )
    to_status: Mapped[HospitalCaseStatus] = mapped_column(
        Enum(
            HospitalCaseStatus,
            values_callable=lambda cls: [member.value for member in cls],
            name="hospital_case_status",
        ),
        primary_key=True,
    )
