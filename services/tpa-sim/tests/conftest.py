from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from claim_contract import signing
from claim_contract.errors import install_handlers
from claim_contract.idempotency import MemoryIdempotencyStore
from claim_contract.inbox import SequenceAction, apply_sequence
from claim_contract.middleware import ContractAuthMiddleware
from claim_contract.samples import make_submission
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from tpa_sim.app import create_app
from tpa_sim.clock import FakeClock
from tpa_sim.config import SimSettings

HOSP_SECRET = "dev-hosp-to-ins-secret-0000000000000000"
INS_SECRET = "dev-ins-to-hosp-secret-0000000000000000"


class Receiver:
    """Stands in for hospital-api: verifies signatures, tracks sequence, records every callback."""

    def __init__(self, now=lambda: datetime.now(UTC)) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.applied: dict[str, int] = {}
        self.fail_next: list[int] = []
        app = FastAPI()
        install_handlers(app)

        def make(kind: str):
            async def h(request: Request):
                if self.fail_next:
                    return JSONResponse({"code": "service_unavailable"}, status_code=self.fail_next.pop(0))
                body = json.loads(request.state.raw_body)
                d = apply_sequence(self.applied.get(body["claim_ref"], 0), int(body["sequence"]))
                if d.action is SequenceAction.apply:
                    self.applied[body["claim_ref"]] = int(body["sequence"])
                self.calls.append((kind, body))
                return JSONResponse({"ok": True})

            return h

        for k in ("status", "queries", "decisions", "settlements"):
            app.post(f"/v1/insurer-callbacks/{k}")(make(k))
        app.add_middleware(
            ContractAuthMiddleware,
            protected_prefixes=["/v1/insurer-callbacks/"],
            secrets=lambda k: [INS_SECRET.encode()] if k == "ins-001" else None,
            idempotency=MemoryIdempotencyStore(),
            now=now,
        )
        self.app = app

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def receiver(clock) -> Receiver:
    return Receiver(now=clock.now)


class Sim:
    def __init__(self, app, client, clock, receiver, cfg) -> None:
        self.app, self.client, self.clock, self.receiver, self.cfg = app, client, clock, receiver, cfg

    def headers(self, method: str, path: str, raw: bytes, idem: str | None = None, extra: dict | None = None) -> dict:
        h = signing.build_headers(HOSP_SECRET.encode(), "hosp-001", method, path, raw, idem or str(uuid.uuid4()), contract_version="1.1")
        return {**h, **(extra or {})}

    async def post(self, path: str, body: dict, idem: str | None = None, extra: dict | None = None) -> httpx.Response:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return await self.client.post(path, content=raw, headers=self.headers("POST", path, raw, idem, extra))

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path, headers=self.headers("GET", path, b""))

    async def submit(self, scenario: str | None = None, **kw) -> dict:
        sub = make_submission(**kw)
        r = await self.post("/v1/hospital-api/claims", sub, extra={"X-Sim-Scenario": scenario} if scenario else None)
        assert r.status_code in (200, 202), r.text
        return sub

    async def run(self, step: float = 60, max_iter: int = 40) -> None:
        """Advance fake time and tick."""
        for _ in range(max_iter):
            self.clock.advance(step)
            await self.app.state.engine.tick()


@pytest.fixture
async def sim(tmp_path, clock, receiver):
    cfg = SimSettings(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sim.db'}")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver.app), base_url="http://hospital")
    app = create_app(cfg, clock=clock, http=http, run_scheduler=False)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim")
        yield Sim(app, client, clock, receiver, cfg)
        await client.aclose()
    await http.aclose()
