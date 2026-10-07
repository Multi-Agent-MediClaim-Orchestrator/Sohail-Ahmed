"""Thin wrapper over ``claim_contract.audit.append`` bound to the insurer's tables (``audit.*``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from claim_contract import audit as _audit
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock

TABLES = _audit.AuditTables()  # audit.audit_event / audit.case_audit_head / audit.audit_anchor


async def append(
    session: AsyncSession,
    case_id: UUID,
    event_type: str,
    *,
    actor_type: str = "system",
    actor_id: str = "insurer-api",
    payload: Mapping[str, Any] | None = None,
    config_versions: Mapping[str, Any] | None = None,
    model_info: Mapping[str, Any] | None = None,
    journey_id: UUID | None = None,
    redis: Any | None = None,
) -> int:
    """Always call inside the transaction that changes business state (state + audit commit atomically)."""
    return await _audit.append(
        session, case_id, actor_type, actor_id, event_type, payload, config_versions, model_info, journey_id,
        tables=TABLES, now=clock.now(), redis=redis,
    )


async def verify(session: AsyncSession, case_id: UUID) -> _audit.VerifyResult:
    return await _audit.verify_chain(session, case_id, tables=TABLES)
