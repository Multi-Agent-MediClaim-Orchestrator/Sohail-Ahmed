"""Simulated pre-authorisation lookup (pre-auth itself is out of scope; arch. §1). Never blocks."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def check(
    session: AsyncSession,
    claim_type: str,
    preauth_ref: str | None,
    member_id: str,
    claimed: Any = None,
) -> list[dict[str, str]]:
    if claim_type != "cashless":
        return []
    if not preauth_ref:
        return [{"code": "preauth_missing", "field": "preauth_ref"}]
    row = (
        await session.execute(
            text("SELECT * FROM simulated_preauth WHERE ref = :r"), {"r": preauth_ref}
        )
    ).first()
    if row is None:
        return [{"code": "preauth_not_found", "field": "preauth_ref"}]
    out: list[dict[str, str]] = []
    if row.member_id != member_id:
        out.append({"code": "preauth_member_mismatch", "field": "preauth_ref"})
    if row.status != "approved" and row.status != "enhanced":
        out.append({"code": f"preauth_{row.status}", "field": "preauth_ref"})
    if row.valid_to < date.today():
        out.append({"code": "preauth_expired", "field": "preauth_ref"})
    return out
