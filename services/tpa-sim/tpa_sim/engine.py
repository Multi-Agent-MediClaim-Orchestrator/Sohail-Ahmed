"""Scenario engine: turns a received claim into scheduled callback events and delivers them (signed, retried, sequenced)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from claim_contract import signing
from claim_contract.enums import INSURER_TO_HOSPITAL, InsurerCaseStatus
from claim_contract.outbox import BACKOFF, MAX_ATTEMPTS, canonical_bytes

from . import dsl
from .chaos import Chaos
from .clock import Clock
from .config import SimSettings
from .store import Store

_NS = uuid.UUID("5f0b2a3e-7a53-4c2f-9d56-0b6a3c1e9a11")
FAR_FUTURE = datetime(9999, 1, 1)
ENDPOINTS = {"status": "/v1/insurer-callbacks/status", "query": "/v1/insurer-callbacks/queries",
             "decision": "/v1/insurer-callbacks/decisions", "settlement": "/v1/insurer-callbacks/settlements"}
TERMINAL_STATES = {"approved", "partially_approved", "rejected", "settled", "closed"}


def _money(v: Decimal | float | str) -> dict[str, str]:
    return {"amount": str(Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)), "currency": "INR"}


def _z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class Engine:
    def __init__(self, store: Store, clock: Clock, settings: SimSettings, scenarios: dict[str, dsl.Scenario], http: httpx.AsyncClient | None = None) -> None:
        self.store, self.clock, self.cfg, self.scenarios = store, clock, settings, scenarios
        self.http = http or httpx.AsyncClient(timeout=10)
        self.chaos = Chaos(seed=settings.chaos_seed)

    # ------------------------------------------------------------------ routing / scheduling
    def pick_scenario(self, sub: dict[str, Any], forced: str | None = None) -> dsl.Scenario:
        if forced and forced in self.scenarios:
            return self.scenarios[forced]
        for sc in self.scenarios.values():
            if dsl.matches(sc, sub):
                return sc
        return self.scenarios[self.cfg.default_scenario]

    async def start(self, ref: str, sc: dsl.Scenario) -> None:
        await self._schedule_step(ref, sc, 0, self.clock.now())

    async def _schedule_step(self, ref: str, sc: dsl.Scenario | None, idx: int, base: datetime) -> None:
        if sc is None or idx >= len(sc.steps):
            await self.store.update_claim(ref, closed=True)
            return
        st = sc.steps[idx]
        due = base + timedelta(seconds=st.after * self.cfg.time_scale)
        await self.store.update_claim(ref, step_cursor=idx)
        if st.kind == "wait_response":
            if st.on_timeout is not None:
                await self.store.add_event(ref, "timeout", due + timedelta(seconds=st.on_timeout * self.cfg.time_scale), {"idx": idx}, st.id)
            return  # resumed by on_query_response / on_withdraw
        if st.manual:
            await self.store.add_event(ref, st.kind, FAR_FUTURE.replace(tzinfo=base.tzinfo), {"idx": idx, "manual": True}, st.id)
            return
        await self.store.add_event(ref, st.kind, due, {"idx": idx}, st.id)

    # ------------------------------------------------------------------ firing
    async def tick(self) -> int:
        """Fire every due event once (shuffled within the reorder window when chaos ``out_of_order`` is on)."""
        due = await self.store.due_events(self.clock.now())
        if self.chaos.out_of_order and len(due) > 1:
            # assign sequence numbers in creation order first, then deliver shuffled
            prepared = []
            for ev in due:
                claim = await self.store.get_claim(ev["claim_ref"])
                if claim and ev["kind"] != "timeout":
                    sc = self.scenarios.get(claim["scenario_id"])
                    idx = int(ev["payload"].get("idx", 0))
                    step = sc.steps[idx] if sc and idx < len(sc.steps) else None
                    if "body" not in ev["payload"]:
                        await self._build(ev, claim, step)
                prepared.append(ev["id"])
            fresh = {e["id"]: e for e in await self.store.due_events(self.clock.now())}
            order = [fresh[i] for i in prepared if i in fresh]
            self.chaos.rng("__tick__").shuffle(order)
            due = order
        n = 0
        for ev in due:
            await self.fire(ev)
            n += 1
        return n

    async def fire(self, ev: dict[str, Any]) -> str:
        ref = ev["claim_ref"]
        claim = await self.store.get_claim(ref)
        synthetic = bool(ev["payload"].get("synthetic"))
        if claim is None or (claim["withdrawn"] and not synthetic):
            await self.store.claim_event(ev["id"], self.clock.now(), "skipped")
            return "skipped"
        retry_attempt = ev["attempts"] > 0
        if not retry_attempt and not await self.store.claim_event(ev["id"], self.clock.now(), "firing"):
            return "already"
        sc = self.scenarios.get(claim["scenario_id"])
        idx = int(ev["payload"].get("idx", claim["step_cursor"]))
        if ev["kind"] == "timeout":
            await self.store.update_event(ev["id"], result="timeout")
            await self._schedule_step(ref, sc, idx + 1, self.clock.now())
            return "timeout"
        step = sc.steps[idx] if sc and idx < len(sc.steps) and not synthetic else None
        if step is None and not synthetic:
            await self.store.update_event(ev["id"], result="no-step")
            return "skipped"
        ev = await self._reload(ev)
        body = ev["payload"].get("body") or await self._build(ev, claim, step)
        probe = step is not None and step.signing != "ok"

        if self.chaos.should_drop(ref, ev["attempts"], MAX_ATTEMPTS):
            ok, status = False, "chaos_dropped"
            await self.store.log(cap=self.cfg.log_cap, ts=self.clock.now().isoformat(), direction="out", claim_ref=ref, method="POST", path=ENDPOINTS[ev["kind"]],
                                 status=0, sequence=body.get("sequence"), chaos="drop", request_body=body, error="dropped")
        else:
            if self.chaos.delay_ms or self.chaos.jitter_ms:
                await asyncio.sleep((self.chaos.delay_ms + self.chaos.rng(ref).random() * self.chaos.jitter_ms) / 1000)
            ok, status = await self._deliver(ev, claim, body, step.signing if step else "ok")  # type: ignore[assignment]
            if (ok or probe) and ((step is not None and step.duplicate) or self.chaos.should_duplicate(ref)):
                await self._deliver(ev, claim, body, "ok", chaos_tag="duplicate")

        now = self.clock.now().isoformat()
        if probe:  # expected to be rejected; never retried, never changes state
            await self.store.update_event(ev["id"], result=f"probe:{status}", attempts=ev["attempts"] + 1, fired_at=now)
            await self._schedule_step(ref, sc, idx + 1, self.clock.now())
            return "probe"
        if ok:
            await self.store.update_event(ev["id"], result="delivered", attempts=ev["attempts"] + 1, fired_at=now)
            if synthetic:
                return "delivered"
            assert step is not None
            if ev["kind"] == "query":
                await self.store.update_claim(ref, state="needs_info")
            elif ev["kind"] == "status":
                await self.store.update_claim(ref, state=step.status)
            elif ev["kind"] == "decision":
                await self.store.update_claim(ref, state={"approve": "approved", "partial": "partially_approved", "reject": "rejected"}[step.outcome], last_decision=body["decision"])
            elif ev["kind"] == "settlement":
                await self.store.update_claim(ref, state="settled" if step.settle == "paid" else "approved")
            await self._schedule_step(ref, sc, idx + 1, self.clock.now())
        else:
            attempts = ev["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                await self.store.update_event(ev["id"], result=f"dead:{status}", attempts=attempts, fired_at=now)
            else:
                delay = BACKOFF[attempts - 1] * self.cfg.retry_scale
                await self.store.update_event(ev["id"], fired_at=None, attempts=attempts, result=f"retry:{status}", due_at=(self.clock.now() + timedelta(seconds=delay)).isoformat())
        return "delivered" if ok else "failed"

    async def _reload(self, ev: dict[str, Any]) -> dict[str, Any]:
        for e in await self.store.events_for(ev["claim_ref"]):
            if e["id"] == ev["id"]:
                return e
        return ev

    async def _build(self, ev: dict[str, Any], claim: dict[str, Any], step: dsl.Step | None) -> dict[str, Any]:
        """Build the callback body once; it is stored on the event so retries re-send identical bytes (same sequence)."""
        ref = claim["claim_ref"]
        pl = ev["payload"]
        probe = step is not None and step.signing != "ok"
        seq = (claim["sequence"] + 1) if probe else (ev.get("seq_assigned") or await self.store.next_sequence(ref))
        await self.store.update_event(ev["id"], seq_assigned=seq)
        now = self.clock.now()
        ino = claim["insurer_claim_no"]
        if ev["kind"] != "status":
            assert step is not None  # only synthetic status events (withdraw notice) have no scenario step
        if ev["kind"] == "status":
            st = InsurerCaseStatus(pl.get("status") or (step.status if step else ""))
            body = {"claim_ref": ref, "insurer_claim_no": ino, "status": st.value, "hospital_visible_status": INSURER_TO_HOSPITAL[st].value, "sequence": seq,
                    "occurred_at": _z(now), "note": pl.get("note") or (step.note if step else None), "open_query_ids": []}
        elif ev["kind"] == "query":
            assert step is not None
            rnd = len(await self.store.queries_for(ref)) + 1
            qid = str(uuid.uuid5(_NS, f"{ref}|q|{rnd}"))
            due_by = now + timedelta(seconds=step.due_in)
            await self.store.add_query({"query_id": qid, "claim_ref": ref, "round": rnd, "category": step.category, "text": step.text,
                                        "requested_doc_types": step.requested_doc_types, "due_by": due_by.isoformat(), "status": "open"})
            body = {"claim_ref": ref, "sequence": seq, "query": {"query_id": qid, "round": min(rnd, 3), "category": step.category, "text": step.text,
                                                                 "requested_doc_types": step.requested_doc_types, "due_by": _z(due_by),
                                                                 "status": "open", "raised_by": "tpa-sim", "grounding": []}}
        elif ev["kind"] == "decision":
            assert step is not None
            claimed = Decimal(claim["payload"]["totals"]["claimed"]["amount"])
            amt = Decimal(0) if step.outcome == "reject" else (claimed if step.outcome == "approve" else claimed * Decimal(str(step.approved_ratio)))
            ded = []
            if step.outcome == "partial" and claimed - amt > 0:
                ded = [{"line_ref": "L1", "rule_id": "SIM-PARTIAL", "amount": _money(claimed - amt), "explanation": "Simulated partial deduction"}]
            body = {"claim_ref": ref, "sequence": seq, "decision": {
                "outcome": step.outcome, "approved_amount": _money(amt), "deductions": ded, "reason_codes": step.reason_codes or [f"SIM_{step.outcome.upper()}"],
                "reviewer_ids": ["REV-SIM-1"], "calc_trace_id": str(uuid.uuid5(_NS, f"{ref}|trace")), "policy_version": 1, "decided_at": _z(now)}}
        else:  # settlement
            assert step is not None
            dec = claim.get("last_decision") or {}
            amt = Decimal((dec.get("approved_amount") or {}).get("amount", "0"))
            n = await self.store.next_counter("utr")
            body = {"claim_ref": ref, "sequence": seq, "settlement": {"settlement_id": str(uuid.uuid5(_NS, f"{ref}|settle|{step.settle}")), "amount": _money(amt),
                                                                       "utr": f"SIMUTR{n:010d}", "paid_on": now.date().isoformat(), "mode": step.mode, "tds": _money(0), "status": step.settle}}
        await self.store.update_event(ev["id"], payload={**pl, "body": body})
        return body

    async def _deliver(self, ev: dict[str, Any], claim: dict[str, Any], body: dict[str, Any], tamper: str = "ok", chaos_tag: str | None = None) -> tuple[bool, int | str]:
        endpoint = ENDPOINTS[ev["kind"]]
        raw = canonical_bytes(body)
        idem = str(uuid.uuid5(_NS, f"event|{ev['id']}"))
        secret = b"wrong-secret-wrong-secret-wrong-secret" if tamper == "bad_secret" else self.cfg.ins_to_hosp_secret.encode()
        now = self.clock.now() + (timedelta(minutes=10) if tamper == "skew_plus_10m" else timedelta())
        headers = signing.build_headers(secret, self.cfg.ins_key_id, "POST", endpoint, raw, idem, contract_version="1.1", now=now)
        try:
            r = await self.http.post(self.cfg.hosp_base_url.rstrip("/") + endpoint, content=raw, headers=headers)
            code: int | str = r.status_code
            ok = 200 <= r.status_code < 300
        except httpx.HTTPError as exc:
            code, ok = type(exc).__name__, False
        await self.store.log(cap=self.cfg.log_cap, ts=self.clock.now().isoformat(), direction="out", claim_ref=claim["claim_ref"], method="POST", path=endpoint,
                             status=code if isinstance(code, int) else 0, sequence=body.get("sequence"), signature_ok=int(tamper == "ok"), idem_replay=0,
                             chaos=chaos_tag or (tamper if tamper != "ok" else None), request_body=body, error=None if ok else str(code))
        return ok, code

    # ------------------------------------------------------------------ hospital -> sim events
    async def on_query_response(self, ref: str, query_id: str, response: dict[str, Any]) -> None:
        await self.store.update_query(query_id, status="answered", response_json=response, answered_at=self.clock.now().isoformat())
        claim = await self.store.get_claim(ref)
        assert claim is not None
        sc = self.scenarios.get(claim["scenario_id"])
        if sc is None:
            return
        idx = claim["step_cursor"]
        if idx < len(sc.steps) and sc.steps[idx].kind == "wait_response":
            await self.store.cancel_pending(ref)  # drop the timeout
            await self._schedule_step(ref, sc, idx + 1, self.clock.now())

    async def on_withdraw(self, ref: str) -> None:
        await self.store.cancel_pending(ref)
        await self.store.update_claim(ref, withdrawn=True, closed=True, state="closed")
        await self.store.add_event(ref, "status", self.clock.now(), {"synthetic": True, "status": "closed", "note": "Withdrawn by hospital"})
