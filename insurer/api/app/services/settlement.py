"""Simulated settlement (03-06): initiate payout, bank callback, retry, reversal, auto-close, reconciliation, reports."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import ProblemError
from claim_contract.models import SettlementNotice
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..clients import bank_sim
from ..db import Tx, sessionmaker, transaction
from ..ids import uuid7
from ..models.core import (
    ClaimCase,
    Decision,
    NetworkHospital,
    Policy,
    PolicyMember,
    Settlement,
    SettlementEvent,
    SettlementTask,
)
from ..settings import Settings, get_settings
from . import audit, cases, events, jobs, outbox, utilisation

log = logging.getLogger("settlement")
CENT = Decimal("0.01")
MODE_MAP = {"neft_sim": "NEFT", "imps_sim": "IMPS", "rtgs_sim": "RTGS"}
RETRYABLE = {"bank_down", "timeout"}
TERMINAL_REASONS = {"limit_exceeded", "invalid_account", "account_frozen"}
SETTLED_STATES = ("approved", "partially_approved")


class PayeeNotConfigured(Exception):
    pass


def assert_sim_mode(s: Settings | None = None) -> None:
    if (s or get_settings()).settlement_mode != "sim":
        raise RuntimeError("INS_SETTLEMENT_MODE must be 'sim'")


# ------------------------------------------------------------------ pure helpers
def net_amount(gross: Decimal, adjustments: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """Returns (net payout, excess). Subtractive adjustments reduce the payout; if they exceed gross the excess is a refund-due flag."""
    sub = sum((Decimal(str(a["amount"])) for a in adjustments if a.get("subtractive")), Decimal("0"))
    raw = gross - sub
    return (max(Decimal("0.00"), raw).quantize(CENT, ROUND_HALF_UP), (-raw).quantize(CENT) if raw < 0 else Decimal("0.00"))


def is_retryable(reason: str | None) -> bool:
    return (reason or "") in RETRYABLE


def retry_delay(attempt: int, base_minutes: int = 5) -> timedelta:
    return timedelta(minutes=base_minutes * attempt)


def valid_utr(utr: str) -> bool:
    return utr.startswith("SIMUTR") and len(utr) >= 12


@jobs.register("initiate_settlement")
async def initiate_job(case_id: str) -> dict[str, Any]:
    return await initiate(UUID(case_id))


# ------------------------------------------------------------------ payee + adjustments
async def resolve_payee(s: AsyncSession, case: ClaimCase) -> dict[str, str]:
    hosp = await s.get(NetworkHospital, case.hospital_id)
    if case.claim_type == "reimbursement":  # member account is synthetic and derived from the member id (SIM prefix)
        import hashlib

        mem = await s.get(PolicyMember, case.member_id) if case.member_id else None
        if mem is None:
            raise PayeeNotConfigured("member not found")
        return {"type": "member", "ref": mem.member_id, "account_hash": hashlib.sha256(f"SIM-MEMBER-ACCOUNT-{mem.member_id}".encode()).hexdigest()}
    if hosp is None or not hosp.account_hash:
        raise PayeeNotConfigured("hospital has no beneficiary account on file")
    return {"type": "hospital", "ref": hosp.hospital_code, "account_hash": hosp.account_hash}


async def adjustments_for(s: AsyncSession, case: ClaimCase) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sub_ref = (await s.execute(text("SELECT submission #>> '{admission,preauth_ref}' FROM core.claim_case WHERE id = :i"), {"i": case.id})).scalar_one()
    if case.claim_type == "cashless" and sub_ref:
        pa = await bank_sim.preauth(sub_ref)
        adv = Decimal(str(pa.get("advance_paid", "0"))) if pa else Decimal("0")
        if adv > 0:
            out.append({"type": "preauth_advance", "amount": f"{adv:.2f}", "ref": sub_ref, "note": "advance paid under pre-authorisation", "subtractive": True})
    return out


# ------------------------------------------------------------------ initiate / send
async def _event(s: AsyncSession, sid: UUID, event: str, detail: dict[str, Any] | None = None) -> None:
    s.add(SettlementEvent(id=uuid7(), settlement_id=sid, event=event, detail=detail))


async def initiate(case_id: UUID, *, settings: Settings | None = None) -> dict[str, Any]:
    s_ = settings or get_settings()
    assert_sim_mode(s_)
    created: Settlement | None = None
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        if case.status not in SETTLED_STATES:
            raise ProblemError("invalid_transition", f"case is {case.status}; settlement starts after an approval", status=409)
        existing = (await s.execute(select(Settlement).where(Settlement.case_id == case_id, Settlement.status.in_(("initiated", "paid", "failed"))))).scalars().first()
        if existing is not None:
            return {"settlement_id": str(existing.id), "status": existing.status, "existing": True}
        amount = case.approved_amount or Decimal("0")
        if amount <= 0:
            await cases.transition(tx, case, InsurerCaseStatus.closed, reason="nothing_to_pay")
            return {"status": "closed", "reason": "nothing_to_pay"}
        try:
            payee = await resolve_payee(s, case)
        except PayeeNotConfigured as exc:
            s.add(SettlementTask(id=uuid7(), case_id=case_id, kind="settlement_failed", detail={"reason": "payee_not_configured", "detail": str(exc)}))
            await audit.append(s, case_id, "settlement.failed", payload={"settlement_id": "none", "reason": "payee_not_configured"})
            return {"status": "task_opened", "reason": "payee_not_configured"}
        adj = await adjustments_for(s, case)
        net, excess = net_amount(amount, adj)
        created = Settlement(id=uuid7(), case_id=case_id, amount=net, gross_amount=amount, adjustments=adj, payee_type=payee["type"], payee_ref=payee["ref"],
                             beneficiary_account_hash=payee["account_hash"], status="initiated", initiated_at=clock.now(), idempotency_key=uuid7(), mode="neft_sim")
        s.add(created)
        await s.flush()
        await _event(s, created.id, "initiated", {"gross": str(amount), "net": str(net)})
        await audit.append(s, case_id, "settlement.initiated", actor_type="system", payload={"settlement_id": str(created.id), "net": str(net), "payee_type": payee["type"]})
        if excess > 0:
            s.add(SettlementTask(id=uuid7(), settlement_id=created.id, case_id=case_id, kind="refund_due", detail={"excess": str(excess)}))
        sid = created.id
    if net == 0:
        return await _settle_zero(sid)
    await send_to_bank(sid)
    return {"settlement_id": str(sid), "status": "initiated"}


async def _settle_zero(sid: UUID) -> dict[str, Any]:
    async with transaction() as tx:
        st = await tx.session.get(Settlement, sid)
        assert st is not None
        st.status, st.paid_at = "paid", clock.now()
        st.utr = f"SIMUTR{clock.now():%Y%m%d}{abs(hash(str(sid))) % 1_000_000:06d}"
        case = await cases.get_for_update(tx.session, st.case_id)
        await cases.transition(tx, case, InsurerCaseStatus.settled, reason="zero_net_payout", notify=False)
    return {"settlement_id": str(sid), "status": "paid"}


async def send_to_bank(sid: UUID, *, settings: Settings | None = None) -> None:
    s_ = settings or get_settings()
    async with sessionmaker()() as s:
        st = await s.get(Settlement, sid)
        if st is None or st.status != "initiated":
            return
        args = (st.id, f"{st.amount:.2f}", st.payee_ref, st.beneficiary_account_hash or "", st.mode, st.idempotency_key)
    try:
        await bank_sim.payout(*args, settings=s_, profile=s_.sim_profile)
        async with transaction() as tx:
            await _event(tx.session, sid, "bank_request_sent")
    except bank_sim.RetryableBankError as exc:
        async with transaction() as tx:
            await _event(tx.session, sid, "bank_unreachable", {"error": str(exc)[:200]})
            st = await tx.session.get(Settlement, sid)
            assert st is not None
            st.next_retry_at = clock.now() + retry_delay(max(1, st.attempt_count + 1), s_.settlement_retry_base_minutes)
    except bank_sim.TerminalBankError as exc:
        await _fail(sid, "unknown", terminal=True, detail=str(exc))


async def _fail(sid: UUID, reason: str, *, terminal: bool = False, detail: str = "") -> None:
    s_ = get_settings()
    async with transaction() as tx:
        st = (await tx.session.execute(select(Settlement).where(Settlement.id == sid).with_for_update())).scalar_one()
        st.attempt_count += 1
        st.status, st.failure_reason = "failed", reason
        retry = (not terminal) and is_retryable(reason) and st.attempt_count < s_.settlement_max_attempts
        if retry:
            st.next_retry_at = clock.now() + retry_delay(st.attempt_count, s_.settlement_retry_base_minutes)
            await _event(tx.session, sid, "retry_scheduled", {"at": st.next_retry_at.isoformat(), "attempt": st.attempt_count})
            await audit.append(tx.session, st.case_id, "settlement.retry_scheduled", payload={"settlement_id": str(sid), "attempt": st.attempt_count})
        else:
            st.next_retry_at = None
            tx.session.add(SettlementTask(id=uuid7(), settlement_id=sid, case_id=st.case_id, kind="settlement_failed", detail={"reason": reason, "attempts": st.attempt_count, "detail": detail}))
            if reason not in TERMINAL_REASONS and reason not in RETRYABLE:
                await audit.append(tx.session, st.case_id, "settlement.anomaly", payload={"settlement_id": str(sid), "kind": f"unknown_failure:{reason}"})
        await _event(tx.session, sid, "failed", {"reason": reason})
        await audit.append(tx.session, st.case_id, "settlement.failed", payload={"settlement_id": str(sid), "reason": reason, "attempt": st.attempt_count})
        cid = st.case_id
        n, rs = st.attempt_count, reason
        tx.on_commit(lambda: events.publish("settlement.failed", cid, {"reason": rs, "attempt": n}, ["reviewer", "senior_reviewer", "admin"]))


# ------------------------------------------------------------------ bank callback
async def on_bank_callback(cb: dict[str, Any]) -> dict[str, Any]:
    sid = UUID(cb["settlement_id"])
    status = cb["status"]
    async with transaction() as tx:
        s = tx.session
        st = (await s.execute(select(Settlement).where(Settlement.id == sid).with_for_update())).scalar_one_or_none()
        if st is None:
            raise ProblemError("settlement_not_committed", "unknown settlement; retry shortly", status=409, retryable=True, headers={"Retry-After": "2"})
        case = await cases.get_for_update(s, st.case_id)
        if status == "paid":
            utr = cb.get("utr") or ""
            if st.status == "paid" and st.utr == utr:
                return {"status": "replay"}
            if st.status == "paid" and st.utr != utr:
                return await _anomaly(tx, st, "duplicate_callback_different_utr", cb)
            if not valid_utr(utr):
                return await _anomaly(tx, st, "invalid_utr", cb)
            if Decimal(str(cb["amount"])) != st.amount:
                return await _anomaly(tx, st, "amount_mismatch", cb)
            paid_at = datetime.fromisoformat(cb["paid_at"].replace("Z", "+00:00")) if cb.get("paid_at") else clock.now()
            st.status, st.utr, st.paid_at = "paid", utr, paid_at
            await _event(s, st.id, "paid", {"utr_hash": _h(utr)})
            await cases.transition(tx, case, InsurerCaseStatus.settled, reason="paid", notify=False)
            await outbox.enqueue_settlement(s, case, _notice(st, "paid"))
            await outbox.enqueue_status(s, case)
            await audit.append(s, case.id, "settlement.paid", payload={"settlement_id": str(st.id), "utr_hash": _h(utr), "amount": str(st.amount)})
            cid, no, u = case.id, case.insurer_claim_no, utr
            tx.on_commit(lambda: events.publish("settlement.paid", cid, {"utr": u}, ["reviewer", "admin"], no))
            return {"status": "applied"}
        if status == "failed":
            if st.status == "paid":
                return await _anomaly(tx, st, "failed_after_paid", cb)
            reason = cb.get("reason") or "unknown"
        elif status == "reversed":
            return await _reverse(tx, st, case, source="bank")
        else:
            raise ProblemError("validation_error", f"unknown callback status {status!r}", status=422)
    await _fail(sid, reason)
    return {"status": "failure_recorded"}


def _h(utr: str) -> str:
    import hashlib

    return hashlib.sha256(utr.encode()).hexdigest()[:16]


def _notice(st: Settlement, status: str) -> SettlementNotice:
    paid_on = (st.paid_at or clock.now()).date()
    return SettlementNotice.from_json_dict({"settlement_id": str(st.id), "amount": {"amount": f"{st.amount:.2f}", "currency": "INR"}, "utr": st.utr or "SIMUTR000000",
                                            "paid_on": paid_on.isoformat(), "mode": MODE_MAP.get(st.mode, "NEFT"), "tds": {"amount": "0.00", "currency": "INR"}, "status": status})


async def _anomaly(tx: Tx, st: Settlement, kind: str, cb: dict[str, Any]) -> dict[str, Any]:
    tx.session.add(SettlementTask(id=uuid7(), settlement_id=st.id, case_id=st.case_id, kind="anomaly", detail={"kind": kind, "callback": {k: v for k, v in cb.items() if k != "payee_account_hash"}}))
    await _event(tx.session, st.id, "anomaly", {"kind": kind})
    await audit.append(tx.session, st.case_id, "settlement.anomaly", payload={"settlement_id": str(st.id), "kind": kind})
    return {"status": "anomaly", "kind": kind}


# ------------------------------------------------------------------ reversal / retry / release
async def _reverse(tx: Tx, st: Settlement, case: ClaimCase, *, source: str) -> dict[str, Any]:
    s = tx.session
    if st.status != "paid":
        raise ProblemError("invalid_state", f"settlement is {st.status}; only a paid settlement can be reversed", status=409)
    st.status = "reversed"
    final = (await s.execute(select(Decision).where(Decision.case_id == case.id, Decision.kind == "final", Decision.status == "finalised").order_by(Decision.created_at.desc()).limit(1))).scalar_one_or_none()
    back = InsurerCaseStatus.approved if (final is None or final.outcome == "approve") else InsurerCaseStatus.partially_approved
    await cases.transition(tx, case, back, reason="settlement_reversed", via_reversal=True, notify=False)
    await outbox.enqueue_settlement(s, case, _notice(st, "reversed"))
    s.add(SettlementTask(id=uuid7(), settlement_id=st.id, case_id=case.id, kind="reversal", detail={"source": source}))
    await _event(s, st.id, "reversed", {"source": source})
    await audit.append(s, case.id, "settlement.reversed", actor_type="system", payload={"settlement_id": str(st.id), "source": source})
    cid, no = case.id, case.insurer_claim_no
    tx.on_commit(lambda: events.publish("settlement.reversed", cid, {}, ["reviewer", "admin"], no))
    return {"status": "reversed"}


async def reverse(settlement_id: UUID, user: Any) -> dict[str, Any]:
    if not get_settings().allow_simulated_reversal:
        raise ProblemError("forbidden", "simulated reversal is disabled in this environment", status=403)
    async with transaction() as tx:
        st = (await tx.session.execute(select(Settlement).where(Settlement.id == settlement_id).with_for_update())).scalar_one_or_none()
        if st is None:
            raise ProblemError("not_found", "settlement not found", status=404)
        case = await cases.get_for_update(tx.session, st.case_id)
        return await _reverse(tx, st, case, source=f"admin:{user.sub}")


async def retry(settlement_id: UUID, user: Any, note: str) -> dict[str, Any]:
    if len(note.strip()) < 5:
        raise ProblemError("validation_error", "a note is required", status=422)
    async with transaction() as tx:
        st = (await tx.session.execute(select(Settlement).where(Settlement.id == settlement_id).with_for_update())).scalar_one_or_none()
        if st is None:
            raise ProblemError("not_found", "settlement not found", status=404)
        if st.status != "failed":
            raise ProblemError("invalid_state", f"settlement is {st.status}; only a failed settlement can be retried", status=409)
        st.status, st.attempt_count, st.next_retry_at, st.idempotency_key, st.failure_reason = "initiated", 0, None, uuid7(), None
        await _event(tx.session, st.id, "manual_retry", {"by": user.sub, "note": note[:200]})
        await tx.session.execute(text("UPDATE core.settlement_task SET status = 'done', closed_at = now(), closed_by = :u WHERE settlement_id = :s AND kind = 'settlement_failed' AND status = 'open'"), {"u": user.sub, "s": st.id})
        sid = st.id
    await send_to_bank(sid)
    return {"status": "initiated"}


async def release_utilisation(settlement_id: UUID, user: Any, reason: str) -> dict[str, Any]:
    if len(reason.strip()) < 5:
        raise ProblemError("validation_error", "a reason is required", status=422)
    async with transaction() as tx:
        s = tx.session
        st = (await s.execute(select(Settlement).where(Settlement.id == settlement_id).with_for_update())).scalar_one_or_none()
        if st is None:
            raise ProblemError("not_found", "settlement not found", status=404)
        if st.status != "reversed" or st.release_utilisation:
            raise ProblemError("invalid_state", "only a reversed settlement can release utilisation, once", status=409)
        case = await s.get(ClaimCase, st.case_id)
        pol = await s.get(Policy, case.policy_id) if case and case.policy_id else None
        if pol is not None:
            adm = (await s.execute(text("SELECT submission #>> '{admission,admitted_on}' FROM core.claim_case WHERE id = :i"), {"i": case.id})).scalar_one()  # type: ignore[union-attr]
            await utilisation.release(s, pol.id, utilisation.policy_year(pol.start_date, date.fromisoformat(adm)), st.gross_amount)
        st.release_utilisation = True
        await _event(s, st.id, "released_utilisation", {"by": user.sub})
        await audit.append(s, st.case_id, "settlement.utilisation_released", actor_type="human", actor_id=user.sub, payload={"settlement_id": str(st.id), "reason": reason[:200]})
        return {"released": str(st.gross_amount)}


# ------------------------------------------------------------------ jobs (cron)
async def retry_due(now: datetime | None = None) -> int:
    """Failed settlements whose retry time has come get a fresh payout attempt (new idempotency key, as the bank saw a failure)."""
    now = now or clock.now()
    async with sessionmaker()() as s:
        ids = [r[0] for r in (await s.execute(text("SELECT id FROM core.settlement WHERE status = 'failed' AND next_retry_at IS NOT NULL AND next_retry_at <= :n"), {"n": now})).all()]
        resend = [r[0] for r in (await s.execute(text("SELECT id FROM core.settlement WHERE status = 'initiated' AND next_retry_at IS NOT NULL AND next_retry_at <= :n"), {"n": now})).all()]
    for sid in ids:
        async with transaction() as tx:
            st = await tx.session.get(Settlement, sid)
            assert st is not None
            st.status, st.next_retry_at, st.idempotency_key = "initiated", None, uuid7()
            await _event(tx.session, sid, "retry", {"attempt": st.attempt_count})
        await send_to_bank(sid)
    for sid in resend:  # sim was unreachable at initiate: same idempotency key
        async with transaction() as tx:
            st = await tx.session.get(Settlement, sid)
            assert st is not None
            st.next_retry_at = None
        await send_to_bank(sid)
    return len(ids) + len(resend)


async def autoclose(now: datetime | None = None) -> int:
    now = now or clock.now()
    days = get_settings().autoclose_days
    async with sessionmaker()() as s:
        rows = (await s.execute(text(
            "SELECT c.id, (SELECT bool_or(o.status = 'sent') FROM ops.outbox o WHERE o.case_id = c.id AND o.endpoint LIKE '%settlements') AS acked, st.paid_at "
            "FROM core.claim_case c JOIN core.settlement st ON st.case_id = c.id AND st.status = 'paid' WHERE c.status::text = 'settled'"))).all()
    n = 0
    for r in rows:
        if r.acked or (r.paid_at and r.paid_at <= now - timedelta(days=days)):
            async with transaction() as tx:
                case = await cases.get_for_update(tx.session, r.id)
                if case.status == "settled":
                    await cases.transition(tx, case, InsurerCaseStatus.closed, reason="settlement_acknowledged" if r.acked else "autoclose_days")
                    n += 1
    return n


async def reconcile(day: date | None = None, since: datetime | None = None) -> dict[str, Any]:
    day = day or clock.now().date()
    ledger_rows = await bank_sim.ledger(day - timedelta(days=1), day + timedelta(days=1))
    async with sessionmaker()() as s:
        ours = (await s.execute(text("SELECT id, utr, amount, status FROM core.settlement WHERE status IN ('paid','reversed') AND utr IS NOT NULL AND (CAST(:since AS timestamptz) IS NULL OR paid_at >= :since)"),
                                {"since": since})).all()
    by_utr = {r["utr"]: r for r in ledger_rows}
    mine = {r.utr: r for r in ours}
    diffs: list[dict[str, Any]] = []
    for utr, r in mine.items():
        b = by_utr.get(utr)
        if b is None:
            diffs.append({"kind": "missing_in_ledger", "utr": utr})
        elif Decimal(str(b["amount"])) != r.amount:
            diffs.append({"kind": "amount_diff", "utr": utr, "ours": str(r.amount), "bank": str(b["amount"])})
        elif b.get("status") not in (r.status, "paid" if r.status == "paid" else "reversed"):
            diffs.append({"kind": "status_diff", "utr": utr, "ours": r.status, "bank": b.get("status")})
    for utr in by_utr.keys() - mine.keys():
        diffs.append({"kind": "missing_in_db", "utr": utr})
    async with transaction() as tx:
        await tx.session.execute(text("INSERT INTO core.reconciliation_run (id, day, kind, diffs) VALUES (:i, :d, 'settlement', CAST(:x AS JSONB))"),
                                 {"i": uuid7(), "d": day, "x": json.dumps(diffs)})
    if diffs:
        await events.publish("settlement.reconcile_mismatch", None, {"count": len(diffs)}, ["admin"])
    return {"diffs": diffs}


async def reconcile_utilisation() -> dict[str, Any]:
    """Σ approved (settled + unsettled) per policy-year must equal ``policy_claim_utilisation``; drift is audited, never auto-fixed."""
    async with sessionmaker()() as s:
        rows = (await s.execute(text(
            "SELECT u.policy_id, u.policy_year, u.utilised_amount, coalesce((SELECT sum(c.approved_amount) FROM core.claim_case c JOIN core.policy p ON p.id = c.policy_id "
            "WHERE c.policy_id = u.policy_id AND c.status::text IN ('approved','partially_approved','settled','closed') AND c.approved_amount > 0 "
            "AND extract(year from age(CAST(c.submission #>> '{admission,admitted_on}' AS date), p.start_date)) + 1 = u.policy_year), 0) AS expected "
            "FROM core.policy_claim_utilisation u"))).all()
    drift = [{"policy_id": str(r.policy_id), "year": r.policy_year, "recorded": str(r.utilised_amount), "expected": str(r.expected)} for r in rows if r.utilised_amount != r.expected]
    if drift:
        async with transaction() as tx:
            case_id = (await tx.session.execute(text("SELECT id FROM core.claim_case LIMIT 1"))).scalar_one_or_none()
            if case_id:
                await audit.append(tx.session, case_id, "reconcile.utilisation", payload={"drift": len(drift)})
    return {"drift": drift}


async def report_csv(day_from: date, day_to: date, tz: str = "Asia/Kolkata") -> str:
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)
    async with sessionmaker()() as s:
        rows = (await s.execute(text(
            "SELECT st.id, c.insurer_claim_no, st.payee_type, st.payee_ref, st.gross_amount, st.amount, st.status, st.utr, st.paid_at, st.initiated_at FROM core.settlement st "
            "JOIN core.claim_case c ON c.id = st.case_id WHERE st.created_at::date BETWEEN :a AND :b ORDER BY st.created_at"), {"a": day_from, "b": day_to})).all()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["settlement_id", "claim_no", "payee_type", "payee_ref", "gross", "net", "status", "utr", "paid_at_ist", "initiated_at_ist"])
    for r in rows:
        w.writerow([r.id, r.insurer_claim_no, r.payee_type, r.payee_ref, f"{r.gross_amount:.2f}", f"{r.amount:.2f}", r.status, r.utr or "",
                    r.paid_at.astimezone(z).strftime("%Y-%m-%d %H:%M:%S") if r.paid_at else "", r.initiated_at.astimezone(z).strftime("%Y-%m-%d %H:%M:%S") if r.initiated_at else ""])
    return buf.getvalue()


_ = uuid4
