"""Scheduled sweeps (n8n cron calls the same functions through ``/internal/*``; the Arq worker schedules them directly).

SLA: a case is flagged ``sla_breached`` once its due time passes (user decision: no 50%/80% escalation ladder)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text

from . import clock, metrics
from .db import sessionmaker, transaction
from .services import audit, events, outbox, settlement
from .settings import get_settings

log = logging.getLogger("cron")
OPEN = ("received", "verifying", "needs_info", "ready_for_decision", "awaiting_approval", "escalated")


async def sla_tick(now: datetime | None = None) -> int:
    now = now or clock.now()
    async with transaction() as tx:
        rows = (await tx.session.execute(text(
            "SELECT id, insurer_claim_no, sla_due_at FROM core.claim_case WHERE sla_breached = false AND sla_due_at IS NOT NULL AND sla_due_at < :n AND status::text = ANY(:o) FOR UPDATE"),
            {"n": now, "o": list(OPEN)})).all()
        for r in rows:
            await tx.session.execute(text("UPDATE core.claim_case SET sla_breached = true WHERE id = :i"), {"i": r.id})
            await audit.append(tx.session, r.id, "sla.breached", payload={"due_at": r.sla_due_at.isoformat()})
            cid, no = r.id, r.insurer_claim_no
            tx.on_commit(lambda cid=cid, no=no: events.publish("sla.breached", cid, {}, ["reviewer", "senior_reviewer", "admin"], no))
    metrics.SLA_BREACH.inc(len(rows))
    return len(rows)


async def purge_ops() -> dict[str, int]:
    async with transaction() as tx:
        a = await tx.session.execute(text("DELETE FROM ops.idempotency_record WHERE created_at < now() - interval '7 days'"))
        b = await tx.session.execute(text("DELETE FROM ops.outbox WHERE status = 'sent' AND sent_at < now() - interval '30 days'"))
    return {"idempotency": a.rowcount, "outbox": b.rowcount}  # type: ignore[attr-defined]


async def refresh_outbox_gauges() -> None:
    async with sessionmaker()() as s:
        metrics.OUTBOX_PENDING.set(int((await s.execute(text("SELECT count(*) FROM ops.outbox WHERE status = 'pending'"))).scalar_one()))
        metrics.OUTBOX_DEAD.set(int((await s.execute(text("SELECT count(*) FROM ops.outbox WHERE status = 'dead'"))).scalar_one()))
        for st, n in (await s.execute(text("SELECT status::text, count(*) FROM core.claim_case GROUP BY 1"))).all():
            metrics.CLAIMS_IN_STATUS.labels("insurer", st).set(int(n))
        for queue, sql in (("review", "SELECT extract(epoch FROM now() - min(updated_at)) FROM core.claim_case WHERE status = 'ready_for_decision'"),
                           ("approval", "SELECT extract(epoch FROM now() - min(updated_at)) FROM core.claim_case WHERE status = 'awaiting_approval'"),
                           ("query", "SELECT extract(epoch FROM now() - min(updated_at)) FROM core.claim_case WHERE status = 'needs_info'")):
            metrics.HUMAN_QUEUE_AGE.labels(queue).set(float((await s.execute(text(sql))).scalar_one() or 0))


async def dispatch_once(http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One outbox pass (callbacks to hospitals) + parked n8n triggers + due settlement retries."""
    from .services import orchestrator

    own = http is None
    http = http or httpx.AsyncClient(timeout=15)
    try:
        sender = outbox.make_sender(sessionmaker(), http, get_settings())
        res = await sender.run_once()
    finally:
        if own:
            await http.aclose()
    n8n = await orchestrator.kick_n8n_triggers()
    retried = await settlement.retry_due()
    await refresh_outbox_gauges()
    return {"delivered": res.delivered, "retried": res.retried, "dead": res.dead, "n8n": n8n, "settlements": retried}


async def run_dispatcher(stop: Any, interval: float = 1.0) -> None:  # pragma: no cover - long running loop
    import asyncio

    while not stop.is_set():
        try:
            await dispatch_once()
        except Exception:
            log.exception("dispatcher pass failed")
        await asyncio.sleep(interval)
