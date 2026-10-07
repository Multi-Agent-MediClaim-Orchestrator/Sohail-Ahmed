"""Policy-year derivation and atomic sum-insured utilisation (01-insurer-db §6.3-6.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ids import uuid7


class SumInsuredExhausted(Exception):
    pass


def policy_year(policy_start: date, admitted_on: date) -> int:
    """1-based policy year containing ``admitted_on`` (renewal anniversaries on the start date; 29 Feb -> 28 Feb)."""
    if admitted_on < policy_start:
        raise ValueError("admission before policy start")

    def anniv(y: int) -> date:
        d = policy_start.day
        if policy_start.month == 2 and d == 29:
            try:
                return date(y, 2, 29)
            except ValueError:
                return date(y, 2, 28)
        return date(y, policy_start.month, d)

    years = admitted_on.year - policy_start.year
    return years + 1 if admitted_on >= anniv(policy_start.year + years) else years


async def utilised(session: AsyncSession, policy_id: UUID, year: int) -> Decimal:
    r = (await session.execute(text("SELECT utilised_amount FROM core.policy_claim_utilisation WHERE policy_id = :p AND policy_year = :y"),
                               {"p": policy_id, "y": year})).scalar_one_or_none()
    return Decimal(r) if r is not None else Decimal("0")


async def apply(session: AsyncSession, policy_id: UUID, year: int, amount: Decimal) -> Decimal:
    """Add ``amount`` to utilisation iff it stays within sum insured + bonus. Raises ``SumInsuredExhausted``."""
    await session.execute(
        text("INSERT INTO core.policy_claim_utilisation (id, policy_id, policy_year, utilised_amount) VALUES (:i, :p, :y, 0) ON CONFLICT (policy_id, policy_year) DO NOTHING"),
        {"i": uuid7(), "p": policy_id, "y": year},
    )
    row = (
        await session.execute(
            text(
                "UPDATE core.policy_claim_utilisation SET utilised_amount = utilised_amount + :a WHERE policy_id = :p AND policy_year = :y "
                "AND utilised_amount + :a <= (SELECT sum_insured + cumulative_bonus FROM core.policy WHERE id = :p) RETURNING utilised_amount"
            ),
            {"a": amount, "p": policy_id, "y": year},
        )
    ).scalar_one_or_none()
    if row is None:
        raise SumInsuredExhausted()
    return Decimal(row)


async def release(session: AsyncSession, policy_id: UUID, year: int, amount: Decimal) -> None:
    await session.execute(
        text("UPDATE core.policy_claim_utilisation SET utilised_amount = GREATEST(utilised_amount - :a, 0) WHERE policy_id = :p AND policy_year = :y"),
        {"a": amount, "p": policy_id, "y": year},
    )
