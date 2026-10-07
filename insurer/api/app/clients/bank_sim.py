"""Bank-sim client (tpa-sim ``/bank/*``). Payments are SIMULATED: the service refuses to start in any other mode."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import httpx
from claim_contract import signing

from .. import clock
from ..settings import Settings, get_settings


class RetryableBankError(Exception):
    pass


class TerminalBankError(Exception):
    pass


_transport: httpx.AsyncBaseTransport | None = None


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def _client(s: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, base_url=s.tpa_sim_url, timeout=10)


def _signed(s: Settings, method: str, path: str, body: bytes, idem: str | None) -> dict[str, str]:
    return signing.build_headers(s.bank_sim_hmac_secret.encode(), s.bank_key_id, method, path, body, idem, contract_version="1.1", now=clock.now())


async def payout(settlement_id: uuid.UUID, amount: str, payee_ref: str, account_hash: str, mode: str, idem: uuid.UUID, *, profile: str | None = None,
                 settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    body = json.dumps({"settlement_id": str(settlement_id), "amount": amount, "payee_ref": payee_ref, "payee_account_hash": account_hash, "mode": mode},
                      separators=(",", ":"), sort_keys=True).encode()
    headers = _signed(s, "POST", "/bank/payouts", body, str(idem))
    if profile:
        headers["X-Sim-Profile"] = profile
    try:
        async with _client(s) as c:
            r = await c.post("/bank/payouts", content=body, headers=headers)
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise RetryableBankError(str(exc)) from exc
    if r.status_code in (200, 202):
        return r.json()  # type: ignore[no-any-return]
    if r.status_code >= 500 or r.status_code in (429, 409):
        raise RetryableBankError(f"bank returned {r.status_code}")
    raise TerminalBankError(f"bank returned {r.status_code}: {r.text[:200]}")


async def ledger(day_from: date, day_to: date, settings: Settings | None = None) -> list[dict[str, Any]]:
    s = settings or get_settings()
    path = f"/bank/ledger?from={day_from.isoformat()}&to={day_to.isoformat()}"
    async with _client(s) as c:
        r = await c.get(path, headers=_signed(s, "GET", path, b"", None))
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


async def preauth(ref: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Simulated pre-authorisation record (for the advance adjustment). ``None`` when unknown or the sim is unreachable."""
    s = settings or get_settings()
    path = f"/preauth/{ref}"
    try:
        async with _client(s) as c:
            r = await c.get(path, headers=_signed(s, "GET", path, b"", None))
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None
