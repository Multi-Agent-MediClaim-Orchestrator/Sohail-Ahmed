"""The ONLY writer of ``claim_case.status`` (03-04 §2.1). Every change: lock row -> assert_transition -> update ->
audit -> (hospital status callback via outbox) in the caller's transaction; SSE event after commit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from claim_contract.enums import INSURER_TO_HOSPITAL, InsurerCaseStatus
from claim_contract.errors import ProblemError
from claim_contract.transitions import assert_insurer_transition
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import Tx
from ..models.core import ClaimCase
from . import audit, events, outbox

TERMINAL = {InsurerCaseStatus.closed}
OPEN_STATES = {InsurerCaseStatus.received, InsurerCaseStatus.verifying, InsurerCaseStatus.needs_info, InsurerCaseStatus.ready_for_decision}


async def get_for_update(session: AsyncSession, case_id: UUID) -> ClaimCase:
    case = (await session.execute(select(ClaimCase).where(ClaimCase.id == case_id).with_for_update())).scalar_one_or_none()
    if case is None:
        raise ProblemError("unknown_claim", "case not found", status=404)
    return case


def check_etag(case: ClaimCase, if_match: str | int | None) -> None:
    if if_match is None:
        return
    expected = str(if_match).strip('"').removeprefix("case-v")
    if str(case.etag) != expected:
        raise ProblemError("stale_etag", "case changed since you loaded it; reload and retry", status=412)


async def transition(
    tx: Tx,
    case: ClaimCase,
    to: InsurerCaseStatus,
    *,
    actor_type: str = "system",
    actor_id: str = "insurer-api",
    reason: str | None = None,
    notify: bool = True,
    via_reversal: bool = False,
    approved_amount: Decimal | None = None,
    note: str | None = None,
) -> ClaimCase:
    cur = InsurerCaseStatus(case.status)
    if cur == to:
        return case
    assert_insurer_transition(cur, to, via_reversal=via_reversal)
    case.status = to.value
    if approved_amount is not None:
        case.approved_amount = approved_amount
    if to is InsurerCaseStatus.closed and reason:
        case.closure_reason = reason
    await tx.session.flush()
    await audit.append(
        tx.session, case.id, "case.status_changed", actor_type=actor_type, actor_id=actor_id,
        payload={"from": cur.value, "to": to.value, "reason": reason or ""}, journey_id=case.journey_id,
    )
    if notify and INSURER_TO_HOSPITAL[cur] != INSURER_TO_HOSPITAL[to]:
        await outbox.enqueue_status(tx.session, case, note=note)
        await tx.session.refresh(case, attribute_names=["last_callback_seq"])
    claim_no, cid, payload = case.insurer_claim_no, case.id, {"from": cur.value, "to": to.value, "reason": reason or ""}

    async def _announce() -> None:
        await events.publish("case.status_changed", cid, payload, insurer_claim_no=claim_no)

    tx.on_commit(_announce)
    return case


def case_summary(case: ClaimCase) -> dict[str, Any]:
    return {"id": str(case.id), "insurer_claim_no": case.insurer_claim_no, "status": case.status, "etag": case.etag}
