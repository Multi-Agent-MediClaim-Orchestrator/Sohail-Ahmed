"""FastAPI surface of the bank simulator (HMAC key ``ins-bank-001``) + signed callbacks to insurer-api."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from claim_contract import signing
from claim_contract.errors import install_handlers
from claim_contract.insurer_side.idempotency import MemoryIdempotencyStore
from claim_contract.insurer_side.middleware import ContractAuthMiddleware
from fastapi import APIRouter, FastAPI, Header, Query, Request

from .bank import BankSim

BANK_KEY_ID = "ins-bank-001"


def make_callback_sender(insurer_base: str, secret: bytes, key_id: str = "bank-sim", transport: httpx.AsyncBaseTransport | None = None):
    """Signed POST to insurer-api ``/internal/settlement/bank-callback``; retries while the settlement row is not committed yet (409)."""

    async def send(body: dict[str, Any]) -> None:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        path = "/internal/settlement/bank-callback"
        for attempt in range(6):
            headers = signing.build_headers(secret, key_id, "POST", path, raw, str(uuid.uuid4()), contract_version="1.1")
            async with httpx.AsyncClient(transport=transport, base_url=insurer_base, timeout=10) as c:
                try:
                    r = await c.post(path, content=raw, headers=headers)
                except httpx.HTTPError:
                    r = None
            if r is not None and r.status_code < 300:
                return
            if r is not None and r.status_code not in (409, 429, 500, 502, 503):
                return  # permanent client error: do not hammer
            import asyncio

            await asyncio.sleep(0.05 * (attempt + 1))

    return send


def bank_router(bank: BankSim) -> APIRouter:
    r = APIRouter()

    @r.post("/bank/payouts", status_code=202)
    async def payouts(request: Request, x_sim_profile: str | None = Header(default=None)) -> dict[str, Any]:
        body = json.loads(request.state.raw_body)
        return await bank.payout(body, request.state.contract_auth.idempotency_key or "", x_sim_profile)

    @r.get("/bank/ledger")
    async def ledger(from_: str | None = Query(default=None, alias="from"), to: str | None = None) -> list[dict[str, Any]]:
        return bank.ledger(from_, to)

    @r.get("/preauth/{ref}")
    async def preauth(ref: str) -> dict[str, Any]:
        rec = bank.get_preauth(ref)
        if rec is None:
            from claim_contract.errors import ProblemError

            raise ProblemError("not_found", "unknown pre-authorisation", status=404)
        return rec

    return r


def build_bank_app(bank: BankSim, secret: bytes) -> FastAPI:
    app = FastAPI(title="tpa-sim bank")
    install_handlers(app)
    app.include_router(bank_router(bank))
    app.add_middleware(ContractAuthMiddleware, protected_prefixes=["/bank/", "/preauth/"], secrets=lambda k: [secret] if k == BANK_KEY_ID else None, idempotency=MemoryIdempotencyStore())
    return app
