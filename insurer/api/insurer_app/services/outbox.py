"""Callback outbox to the hospital (03-02 §6.3). Rows are written in the business transaction; delivery uses the
shared ``OutboxSender`` (signing, retries, backoff, dead-letter) with per-claim ordering (lowest pending seq only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from claim_contract import signing  # noqa: F401
from claim_contract.insurer_side.outbox import OutboxItem, OutboxSender
from claim_contract.models import (
    DecisionCallback,
    Query,
    QueryCallback,
    SettlementCallback,
    StatusUpdate,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import clock
from ..ids import uuid7
from ..models.core import ClaimCase, NetworkHospital, OutboxRow
from ..settings import Settings, get_settings
from . import audit, events

ENDPOINTS = {
    "status": "/v1/insurer-callbacks/status",
    "queries": "/v1/insurer-callbacks/queries",
    "decisions": "/v1/insurer-callbacks/decisions",
    "settlements": "/v1/insurer-callbacks/settlements",
}


async def next_sequence(session: AsyncSession, case_id: UUID) -> int:
    """Allocate the next per-claim callback sequence (row lock serialises concurrent enqueues)."""
    return int(
        (
            await session.execute(
                text("UPDATE core.claim_case SET last_callback_seq = last_callback_seq + 1 WHERE id = :i RETURNING last_callback_seq"),
                {"i": case_id},
            )
        ).scalar_one()
    )


async def enqueue(session: AsyncSession, case: ClaimCase, kind: str, build: Any, *, base_url: str | None = None) -> OutboxRow:
    """``build(seq) -> contract model`` (validated before it is stored). Returns the outbox row."""
    seq = await next_sequence(session, case.id)
    model = build(seq)
    row = OutboxRow(
        id=uuid7(), case_id=case.id, endpoint=ENDPOINTS[kind], payload=model.model_dump(mode="json"), seq=seq,
        idempotency_key=uuid7(), status="pending", kind="callback", next_attempt_at=clock.now(), base_url=base_url,
    )
    session.add(row)
    return row


def status_update(case: ClaimCase, seq: int, *, note: str | None = None, open_query_ids: list[UUID] | None = None, decision: Any = None) -> StatusUpdate:
    from claim_contract.enums import INSURER_TO_HOSPITAL, InsurerCaseStatus

    st = InsurerCaseStatus(case.status)
    return StatusUpdate(
        claim_ref=case.hospital_claim_ref, insurer_claim_no=case.insurer_claim_no, status=st,
        hospital_visible_status=INSURER_TO_HOSPITAL[st], sequence=seq, occurred_at=clock.now(), note=note,
        open_query_ids=open_query_ids or [], decision=decision,
    )


async def enqueue_status(session: AsyncSession, case: ClaimCase, **kw: Any) -> OutboxRow:
    h = await session.get(NetworkHospital, case.hospital_id)
    return await enqueue(session, case, "status", lambda seq: status_update(case, seq, **kw), base_url=h.callback_base_url if h else None)


async def enqueue_query(session: AsyncSession, case: ClaimCase, query: Query) -> OutboxRow:
    h = await session.get(NetworkHospital, case.hospital_id)
    return await enqueue(session, case, "queries", lambda seq: QueryCallback(claim_ref=case.hospital_claim_ref, sequence=seq, query=query),
                         base_url=h.callback_base_url if h else None)


async def enqueue_decision(session: AsyncSession, case: ClaimCase, decision: Any) -> OutboxRow:
    h = await session.get(NetworkHospital, case.hospital_id)
    return await enqueue(session, case, "decisions", lambda seq: DecisionCallback(claim_ref=case.hospital_claim_ref, sequence=seq, decision=decision),
                         base_url=h.callback_base_url if h else None)


async def enqueue_settlement(session: AsyncSession, case: ClaimCase, notice: Any) -> OutboxRow:
    h = await session.get(NetworkHospital, case.hospital_id)
    return await enqueue(session, case, "settlements", lambda seq: SettlementCallback(claim_ref=case.hospital_claim_ref, sequence=seq, settlement=notice),
                         base_url=h.callback_base_url if h else None)


class InsurerOutboxRepo:
    """Adapter from ``ops.outbox`` to the shared ``OutboxRepo`` protocol."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], journey_lookup: bool = True) -> None:
        self.sm = sm

    async def fetch_due(self, limit: int) -> list[OutboxItem]:
        async with self.sm() as s, s.begin():
            rows = (
                await s.execute(
                    text(
                        "SELECT DISTINCT ON (o.case_id) o.id, o.endpoint, o.payload, o.idempotency_key, o.seq, o.attempts, o.base_url, o.next_attempt_at, c.hospital_claim_ref, c.journey_id "
                        "FROM ops.outbox o JOIN core.claim_case c ON c.id = o.case_id "
                        "WHERE o.status = 'pending' AND o.kind = 'callback' ORDER BY o.case_id, o.seq"
                    )
                )
            ).all()
            now = clock.now()
            due = [r for r in rows if r.next_attempt_at <= now][:limit]  # lowest pending seq per case, only if due
            return [
                OutboxItem(id=r.id, endpoint=r.endpoint, body=r.payload if isinstance(r.payload, dict) else __import__("json").loads(r.payload),
                           idempotency_key=str(r.idempotency_key), claim_ref=r.hospital_claim_ref, sequence=r.seq, attempts=r.attempts,
                           base_url=r.base_url, journey_id=str(r.journey_id) if r.journey_id else None)
                for r in due
            ]

    async def _set(self, item: OutboxItem, **cols: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        async with self.sm() as s, s.begin():
            await s.execute(text(f"UPDATE ops.outbox SET {sets} WHERE id = :_id"), {**cols, "_id": item.id})

    async def mark_delivered(self, item: OutboxItem) -> None:
        await self._set(item, status="sent", sent_at=clock.now(), attempts=item.attempts)
        async with self.sm() as s, s.begin():  # query delivery ack -> acked_at (05: due dates run from the ack)
            await s.execute(
                text("UPDATE core.query SET acked_at = now() WHERE callback_seq = :q AND case_id = (SELECT case_id FROM ops.outbox WHERE id = :i)"),
                {"q": item.sequence, "i": item.id},
            )

    async def reschedule(self, item: OutboxItem, next_attempt_at: datetime, error: str) -> None:
        await self._set(item, attempts=item.attempts, next_attempt_at=next_attempt_at, last_error=error[:500])

    async def mark_dead(self, item: OutboxItem, error: str) -> None:
        await self._set(item, status="dead", attempts=item.attempts, last_error=error[:500])
        async with self.sm() as s, s.begin():
            case_id = (await s.execute(text("SELECT case_id FROM ops.outbox WHERE id = :i"), {"i": item.id})).scalar_one()
            await audit.append(s, case_id, "outbox.dead", payload={"endpoint": item.endpoint, "attempts": item.attempts})
        await events.publish("delivery.dead_letter", None, {"endpoint": item.endpoint, "attempts": item.attempts}, ["admin"])


def make_sender(sm: async_sessionmaker[AsyncSession], http: httpx.AsyncClient, settings: Settings | None = None, *, backoff_scale: float = 1.0) -> OutboxSender:
    s = settings or get_settings()
    return OutboxSender(InsurerOutboxRepo(sm), http, base_url=s.hospital_callback_base, key_id=s.ins_key_id,
                        secret=s.ins_to_hosp_hmac_secret.encode(), backoff_scale=backoff_scale)


async def pending_count(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM ops.outbox WHERE status = 'pending'"))).scalar_one())


async def dead_rows(session: AsyncSession) -> list[OutboxRow]:
    return list((await session.execute(select(OutboxRow).where(OutboxRow.status == "dead"))).scalars())


_ = UTC
