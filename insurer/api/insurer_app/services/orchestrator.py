"""Verification orchestration. ``inline`` runs the pipeline in-process (dev/tests, no n8n); ``n8n`` fires the
``verification-start`` webhook and lets the flow sequence the steps via the internal API (09). Both paths call the same
service functions, so behaviour is identical — n8n holds no business rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import Any
from uuid import UUID

import httpx
from claim_contract.enums import StepName
from sqlalchemy import select, text

from .. import clock
from ..clients import crew
from ..config.service import ConfigService
from ..db import sessionmaker, transaction
from ..ids import uuid7
from ..models.core import ClaimCase, ClaimDocument
from ..settings import get_settings
from ..verification.schemas import AgentInfo, RunStart, StepResultBody
from . import context, docs_fetch, jobs, verification

log = logging.getLogger("orchestrator")
cfg: ConfigService | None = None
_n8n_transport: httpx.AsyncBaseTransport | None = None


def configure(config: ConfigService) -> None:
    global cfg
    cfg = config


def set_n8n_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _n8n_transport
    _n8n_transport = t


def _cfg() -> ConfigService:
    global cfg
    if cfg is None:
        cfg = ConfigService()
    return cfg


@jobs.register("start_verification")
async def start_verification(case_id: str, trigger: str = "initial", steps: list[str] | None = None, client_token: str | None = None) -> dict[str, Any]:
    s = get_settings()
    token = client_token or str(uuid.uuid4())
    if s.orchestrator == "n8n":
        return await trigger_n8n(case_id, trigger, steps or ["all"], token)
    return await run_inline(UUID(case_id), trigger, steps or ["all"], token)


# ------------------------------------------------------------------ n8n webhook (with durable retry row)
def sign_webhook(body: bytes) -> str:
    return "sha256=" + hmac.new(get_settings().n8n_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


async def trigger_n8n(case_id: str, trigger: str, steps: list[str], client_token: str) -> dict[str, Any]:
    s = get_settings()
    payload = {"case_id": case_id, "trigger": trigger, "steps": steps, "client_token": client_token}
    body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        async with httpx.AsyncClient(transport=_n8n_transport, timeout=10) as c:
            r = await c.post(f"{s.n8n_url.rstrip('/')}/webhook/verification-start", content=body, headers={
                "Content-Type": "application/json", "X-N8N-Signature": sign_webhook(body), "X-Event-Id": str(uuid.uuid4()), "X-Client-Token": client_token})
        if r.status_code < 300:
            return {"triggered": True}
        raise httpx.HTTPError(f"n8n {r.status_code}")
    except httpx.HTTPError as exc:  # never fail the receipt: park a retry row (kind=n8n_trigger)
        async with transaction() as tx:
            from ..models.core import OutboxRow

            seq = int((await tx.session.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM ops.outbox WHERE case_id = :c"), {"c": UUID(case_id)})).scalar_one())
            tx.session.add(OutboxRow(id=uuid7(), case_id=UUID(case_id), endpoint="n8n:verification-start", payload=payload, seq=seq, idempotency_key=uuid7(),
                                     status="pending", kind="n8n_trigger", last_error=str(exc)[:300], next_attempt_at=clock.now()))
        return {"triggered": False, "queued_retry": True}


async def kick_n8n_triggers() -> int:
    """Retry parked n8n triggers (cron sweep ``/internal/outbox/kick``)."""
    sm = sessionmaker()
    async with sm() as s:
        rows = (await s.execute(text("SELECT id, payload FROM ops.outbox WHERE kind = 'n8n_trigger' AND status = 'pending' AND next_attempt_at <= now() ORDER BY created_at LIMIT 20"))).all()
    done = 0
    for r in rows:
        p = r.payload if isinstance(r.payload, dict) else json.loads(r.payload)
        res = await trigger_n8n(p["case_id"], p["trigger"], p["steps"], p["client_token"])
        if res.get("triggered"):
            async with transaction() as tx:
                await tx.session.execute(text("UPDATE ops.outbox SET status = 'sent', sent_at = now() WHERE id = :i"), {"i": r.id})
            done += 1
    return done


# ------------------------------------------------------------------ inline pipeline
async def _ensure_documents(case_id: UUID) -> None:
    async with sessionmaker()() as s:
        pending = (await s.execute(select(ClaimDocument.id).where(ClaimDocument.case_id == case_id, ClaimDocument.fetch_status == "pending"))).scalars().all()
    for doc_id in pending:
        await docs_fetch.fetch_document(doc_id)


async def run_inline(case_id: UUID, trigger: str, steps: list[str], client_token: str | None) -> dict[str, Any]:
    c = _cfg()
    await _ensure_documents(case_id)
    try:
        summary = await verification.start_run(c, case_id, RunStart(trigger=trigger, steps=steps, client_token=client_token), actor="inline-orchestrator")
    except Exception as exc:
        log.info("run not started: %s", exc)
        raise
    if summary["existing"]:
        return summary
    run_id = UUID(summary["run_id"])
    pending = list(summary["steps_to_run"])
    step = pending[0] if pending else "finalize"
    guard = 0
    while step != "finalize" and guard < 12:
        guard += 1
        await verification.mark_step_running(run_id, step)
        body = await _agent_body(c, case_id, step)
        res = await verification.record_step(c, run_id, step, body, actor="inline-orchestrator")
        step = res["next_step"]
    out = await verification.finalize_run(c, run_id, actor="inline-orchestrator")
    return {**summary, **out}


AGENT_PATHS = {"identity": crew.PATH_IDENTITY, "authenticity": crew.PATH_AUTHENTICITY, "coverage": crew.PATH_COVERAGE}


async def _agent_body(c: ConfigService, case_id: UUID, step: str) -> StepResultBody | None:
    """Ask the crew to explain; any failure degrades (never blocks) — the deterministic result is computed by the API."""
    if not crew.enabled() or step not in (*AGENT_PATHS, "calculation"):
        return None
    async with sessionmaker()() as s:
        case = await s.get(ClaimCase, case_id)
        assert case is not None
        vctx = await context.build_vctx(s, c, case)
    try:
        if step == "calculation":
            from ..models.core import BillLine

            async with sessionmaker()() as s:
                lines = (await s.execute(select(BillLine).where(BillLine.case_id == case_id).order_by(BillLine.line_no))).scalars().all()
            raw = [{"line_ref": f"L{ln.line_no:04d}", "category": ln.category, "description": ln.description, "qty": str(ln.qty), "unit_price": f"{ln.unit_price:.2f}",
                    "amount": f"{ln.amount:.2f}"} for ln in lines]
            out = await crew.call(crew.PATH_CALC_MAP, case_id, {"lines": raw, "product_code": vctx.policy.product_code if vctx.policy else ""})
            mapping = {m["line_ref"]: {"mapped_group": m["mapped_group"], "procedure_group": m.get("procedure_group"), "tags": m.get("tags", []),
                                       "is_non_medical": m.get("is_non_medical", False), "is_implant": m.get("is_implant", False), "source": m.get("source", "agent")}
                       for m in out.get("lines", [])}
            return StepResultBody(step=StepName.calculation, agent=AgentInfo(name="calc_mapper", prompt_version=out.get("prompt_version", ""), trace_id=out.get("trace_id")),
                                  line_mapping=mapping, degraded=bool(out.get("degraded")))
        out = await crew.call(AGENT_PATHS[step], case_id, context.minimised_context(vctx, step))
        notes = out.get("reconciliation_notes") or "; ".join(out.get("explanations", []) or []) or out.get("waiting_notes")
        return StepResultBody(step=StepName(step), agent=AgentInfo(name=f"{step}_agent", prompt_version=out.get("prompt_version", ""), trace_id=out.get("trace_id")),
                              agent_notes=notes, degraded=bool(out.get("degraded")), agent_output={k: v for k, v in out.items() if k not in ("token_usage",)})
    except crew.CrewUnavailable:
        return StepResultBody(step=StepName(step), failure="agent_unavailable", degraded=True)
    except crew.CrewInvalidOutput:
        return StepResultBody(step=StepName(step), failure="agent_invalid_output", degraded=True)
