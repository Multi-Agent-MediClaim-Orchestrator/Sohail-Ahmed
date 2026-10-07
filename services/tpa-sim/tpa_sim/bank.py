"""Bank simulator (03-06 §4.2): payouts, scripted callback profiles, ledger and simulated pre-authorisation records.

Profiles (header ``X-Sim-Profile`` or ``BankSim.default_profile``): always_pay, fail_once_then_pay, always_fail[:reason], reverse_after_pay,
duplicate_callback, duplicate_callback_diff_utr, amount_mismatch, slow_5min (callback held until ``release``)."""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

Callback = Callable[[dict[str, Any]], Awaitable[None]]
_UTR_COUNTER = itertools.count(int(time.time()) % 900_000)  # process-wide so several simulators never mint the same UTR


@dataclass
class Payout:
    payout_id: str
    settlement_id: str
    amount: str
    payee_ref: str
    account_hash: str
    mode: str
    idem: str
    profile: str
    attempt: int
    utr: str | None = None
    status: str = "queued"
    paid_at: str | None = None


@dataclass
class BankSim:
    callback: Callback | None = None
    default_profile: str = "always_pay"
    delay_s: float = 0.0
    payouts: dict[str, Payout] = field(default_factory=dict)  # by idempotency key
    attempts: dict[str, int] = field(default_factory=dict)  # by settlement id
    ledger_rows: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    preauth: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent_callbacks: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def next_utr(self) -> str:
        return f"SIMUTR{datetime.now(UTC):%Y%m%d}{next(_UTR_COUNTER) % 1_000_000:06d}"

    async def payout(self, body: dict[str, Any], idem: str, profile: str | None = None) -> dict[str, Any]:
        """Idempotent on the idempotency key: a replay returns the first payout and never pays twice."""
        if idem in self.payouts:
            p = self.payouts[idem]
            return {"payout_id": p.payout_id, "status": p.status, "replay": True}
        sid = body["settlement_id"]
        self.attempts[sid] = self.attempts.get(sid, 0) + 1
        p = Payout(f"PO-{uuid.uuid4().hex[:10]}", sid, body["amount"], body["payee_ref"], body.get("payee_account_hash", ""), body.get("mode", "neft_sim"), idem,
                   profile or self.default_profile, self.attempts[sid])
        self.payouts[idem] = p
        self._spawn(self._run(p))
        return {"payout_id": p.payout_id, "status": "queued"}

    def _spawn(self, coro: Awaitable[None]) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def settle_all(self) -> None:
        """Wait for scheduled callbacks (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _emit(self, cb: dict[str, Any]) -> None:
        self.sent_callbacks.append(cb)
        if self.callback is not None:
            await self.callback(cb)

    def _paid(self, p: Payout, amount: str | None = None, utr: str | None = None) -> dict[str, Any]:
        p.utr = utr or p.utr or self.next_utr()
        p.status, p.paid_at = "paid", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not any(r["utr"] == p.utr for r in self.ledger_rows):
            self.ledger_rows.append({"utr": p.utr, "settlement_id": p.settlement_id, "amount": p.amount, "status": "paid", "paid_at": p.paid_at})
        return {"settlement_id": p.settlement_id, "utr": p.utr, "status": "paid", "amount": amount or p.amount, "paid_at": p.paid_at}

    async def _run(self, p: Payout) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        name, _, arg = p.profile.partition(":")
        if name == "slow_5min":
            p.status = "queued"
            self.held.append({"payout": p})
            return
        if name == "fail_once_then_pay" and p.attempt == 1:
            p.status = "failed"
            await self._emit({"settlement_id": p.settlement_id, "status": "failed", "reason": "bank_down", "amount": p.amount})
            return
        if name == "always_fail":
            p.status = "failed"
            await self._emit({"settlement_id": p.settlement_id, "status": "failed", "reason": arg or "bank_down", "amount": p.amount})
            return
        if name == "amount_mismatch":
            cb = self._paid(p, amount=str(Decimal(p.amount) - Decimal("1.00")))
            await self._emit(cb)
            return
        cb = self._paid(p)
        await self._emit(cb)
        if name == "duplicate_callback":
            await self._emit(dict(cb))
        elif name == "duplicate_callback_diff_utr":
            await self._emit({**cb, "utr": self.next_utr()})
        elif name == "reverse_after_pay":
            for r in self.ledger_rows:
                if r["utr"] == p.utr:
                    r["status"] = "reversed"
            await self._emit({"settlement_id": p.settlement_id, "utr": p.utr, "status": "reversed", "amount": p.amount})

    async def release(self) -> int:
        """Deliver callbacks held by ``slow_5min``."""
        held, self.held = self.held, []
        for h in held:
            p: Payout = h["payout"]
            await self._emit(self._paid(p))
        return len(held)

    def ledger(self, date_from: str | None = None, date_to: str | None = None) -> list[dict[str, Any]]:
        return list(self.ledger_rows)

    def get_preauth(self, ref: str) -> dict[str, Any] | None:
        return self.preauth.get(ref)
