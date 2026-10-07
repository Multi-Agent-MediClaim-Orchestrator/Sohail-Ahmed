"""Reviewer assignment (03-03 §6.11): least-loaded active reviewer with matching skills; senior for high value."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock


@dataclass(frozen=True)
class Reviewer:
    sub: str
    roles: tuple[str, ...]
    skill_tags: frozenset[str]
    active: bool = True
    on_leave: bool = False
    last_assigned_at: datetime | None = None


def required_tags(claim_type: str, claimed: Decimal, t_auto: Decimal) -> set[str]:
    tags = {claim_type}
    if claimed > t_auto:
        tags.add("high_value")
    return tags


def pick_assignee(claim_type: str, claimed: Decimal, t_auto: Decimal, reviewers: list[Reviewer], loads: dict[str, int], busy: set[str] | None = None) -> str | None:
    need = required_tags(claim_type, claimed, t_auto)
    pool = [r for r in reviewers if r.active and not r.on_leave and "reviewer" in r.roles and need <= r.skill_tags]
    if claimed > t_auto:
        seniors = [r for r in pool if "senior_reviewer" in r.roles]
        pool = seniors or pool
    if busy:  # never reassign away from a reviewer who is actively viewing
        pass
    if not pool:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=clock.now().tzinfo)
    return min(pool, key=lambda r: (loads.get(r.sub, 0), r.last_assigned_at or epoch, r.sub)).sub


async def load_reviewers(session: AsyncSession) -> tuple[list[Reviewer], dict[str, int]]:
    rows = (await session.execute(text("SELECT sub, roles, skill_tags, active, on_leave, last_assigned_at FROM ops.user_profile WHERE 'reviewer' = ANY(roles)"))).all()
    revs = [Reviewer(r.sub, tuple(r.roles), frozenset(r.skill_tags or []), r.active, r.on_leave, r.last_assigned_at) for r in rows]
    loads = {r.assigned_reviewer: r.open_cases for r in (await session.execute(text("SELECT assigned_reviewer, open_cases FROM core.v_reviewer_load"))).all()}
    return revs, loads


async def assign_if_needed(session: AsyncSession, case: Any, t_auto: Decimal) -> str | None:
    if case.assigned_reviewer:
        return case.assigned_reviewer
    revs, loads = await load_reviewers(session)
    sub = pick_assignee(case.claim_type, case.claimed_amount, t_auto, revs, loads)
    if sub:
        case.assigned_reviewer = sub
        await session.execute(text("UPDATE ops.user_profile SET last_assigned_at = now() WHERE sub = :s"), {"s": sub})
    return sub
