"""Mirrors migrations (generated from the migrated schema, then split by area)."""

import datetime
import decimal

from sqlalchemy import Boolean, CheckConstraint, Date, Index, Numeric, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NetworkInsurer(Base):
    __tablename__ = "network_insurer"
    __table_args__ = (PrimaryKeyConstraint("insurer_name", name="network_insurer_pkey"),)

    insurer_name: Mapped[str] = mapped_column(Text, primary_key=True)
    cashless_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class SimulatedPreauth(Base):
    __tablename__ = "simulated_preauth"
    __table_args__ = (
        CheckConstraint(
            "status = ANY (ARRAY['approved'::text, 'enhanced'::text, 'cancelled'::text, 'expired'::text])",
            name="ck_simulated_preauth_status",
        ),
        CheckConstraint("valid_to >= valid_from", name="ck_simulated_preauth_validity"),
        PrimaryKeyConstraint("ref", name="simulated_preauth_pkey"),
        Index("ix_simulated_preauth_member", "member_id"),
    )

    ref: Mapped[str] = mapped_column(Text, primary_key=True)
    member_id: Mapped[str] = mapped_column(Text, nullable=False)
    insurer_name: Mapped[str] = mapped_column(Text, nullable=False)
    approved_amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    valid_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
