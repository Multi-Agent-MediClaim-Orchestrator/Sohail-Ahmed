"""tpa-sim FastAPI app: contract endpoints (``/v1/hospital-api/*``), bank sim, control plane (``/sim/*``)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from claim_contract.errors import ProblemError, install_handlers
from claim_contract.idempotency import MemoryIdempotencyStore
from claim_contract.middleware import ContractAuthMiddleware, MemoryRateLimiter
from claim_contract.models import ClaimSubmission, QueryResponse, WithdrawRequest
from claim_contract.validation import validate_submission
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from . import dsl
from .bank import BankSim
from .bank_api import BANK_KEY_ID, bank_router
from .clock import Clock
from .config import SimSettings, get_settings
from .engine import Engine
from .scenarios import builtin_scenarios
from .store import Store


def _key_id(request: Request) -> str:
    return request.state.contract_auth.key_id


def create_app(settings: SimSettings | None = None, *, clock: Clock | None = None, http: httpx.AsyncClient | None = None, run_scheduler: bool = True) -> FastAPI:
    cfg = settings or get_settings()
    clock = clock or Clock()
    store = Store(cfg.db_url)
    bank = BankSim()
    scenarios: dict[str, dsl.Scenario] = {}
    engine = Engine(store, clock, cfg, scenarios, http)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await store.init()
        for sc in builtin_scenarios() + dsl.load_dir(cfg.scenarios_dir):
            scenarios[sc.id] = sc
            await store.upsert_scenario(sc.id, sc.name, sc.description, [s.model_dump() for s in sc.steps], sc.match, "builtin")
        for row in await store.scenarios():
            if row["source"] == "custom" and row["id"] not in scenarios:
                scenarios[row["id"]] = dsl.Scenario(id=row["id"], name=row["name"], description=row["description"], steps=row["steps"], match=row["match"] or {})
        task = asyncio.create_task(_loop()) if run_scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            await store.close()

    async def _loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await engine.tick()
            await asyncio.sleep(1)

    app = FastAPI(title="tpa-sim", version="1.0", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.clock, app.state.bank, app.state.scenarios = store, engine, clock, bank, scenarios
    install_handlers(app)

    async def chaos_gate() -> None:
        code = engine.chaos.take_failure()
        if code:
            raise ProblemError("service_unavailable", "simulated outage (chaos fail_status)", status=code)

    # ------------------------------------------------------------------ contract endpoints
    @app.post("/v1/hospital-api/claims", status_code=202, dependencies=[Depends(chaos_gate)])
    async def submit(request: Request, x_sim_scenario: str | None = Header(default=None)) -> JSONResponse:
        raw = request.state.raw_body
        try:
            sub = ClaimSubmission.model_validate_json(raw)
        except ValidationError as exc:
            raise ProblemError("validation_error", "request body failed validation", errors=[]) from exc
        report = validate_submission(sub)
        if report.errors:
            raise ProblemError("validation_error", report.errors[0].message)
        ref = sub.claim_ref
        existing = await store.get_claim(ref)
        phash = hashlib.sha256(raw).hexdigest()
        if existing:
            if existing["payload_hash"] != phash:
                raise ProblemError("duplicate_claim", "claim_ref already submitted with different content", status=409)
            return JSONResponse(_ack(existing), status_code=200, headers={"Idempotent-Replay": "true"})
        payload = json.loads(raw)
        sc = engine.pick_scenario(payload, x_sim_scenario if cfg.expose_scenario else None)
        n = await store.next_counter("claim_no")
        ino = f"IC-{clock.now().year}-{n:06d}"
        row = {"claim_ref": ref, "insurer_claim_no": ino, "scenario_id": sc.id, "state": "received", "round": 0, "sequence": 0, "step_cursor": 0,
               "received_at": clock.now().isoformat(), "idem_key": request.state.contract_auth.idempotency_key or "", "payload_hash": phash, "payload": payload}
        await store.add_claim(row)
        await engine.start(ref, sc)
        return JSONResponse(_ack(row), status_code=202)

    def _ack(row: dict[str, Any]) -> dict[str, Any]:
        return {"claim_ref": row["claim_ref"], "insurer_claim_no": row["insurer_claim_no"], "status": "received", "received_at": row["received_at"].replace("+00:00", "Z"),
                "sequence": 0, "document_ingest": {"queued": len(row["payload"].get("documents", [])), "failed": 0}, "contract_version": "1.1"}

    async def _claim(ref: str) -> dict[str, Any]:
        c = await store.get_claim(ref)
        if c is None:
            raise ProblemError("not_found", "unknown claim_ref", status=404)
        return c

    @app.get("/v1/hospital-api/claims/{ref}", dependencies=[Depends(chaos_gate)])
    async def status(ref: str, include: str | None = None) -> dict[str, Any]:
        c = await _claim(ref)
        from claim_contract.enums import INSURER_TO_HOSPITAL, InsurerCaseStatus

        st = c["state"]
        ist = InsurerCaseStatus(st) if st in InsurerCaseStatus._value2member_map_ else InsurerCaseStatus.verifying
        qs = await store.queries_for(ref)
        out = {"claim_ref": ref, "insurer_claim_no": c["insurer_claim_no"], "status": ist.value, "hospital_visible_status": INSURER_TO_HOSPITAL[ist].value,
               "sequence": c["sequence"], "open_query_ids": [q["query_id"] for q in qs if q["status"] == "open"], "decision": c["last_decision"]}
        if include == "queries":
            out["queries"] = qs
        return out

    @app.get("/v1/hospital-api/claims/{ref}/queries", dependencies=[Depends(chaos_gate)])
    async def list_queries(ref: str) -> dict[str, Any]:
        await _claim(ref)
        return {"items": await store.queries_for(ref), "next_cursor": None}

    @app.post("/v1/hospital-api/claims/{ref}/documents", status_code=202, dependencies=[Depends(chaos_gate)])
    async def docs(ref: str, request: Request) -> dict[str, Any]:
        await _claim(ref)
        body = json.loads(request.state.raw_body)
        qid, answered = body.get("query_id"), False
        if body.get("query_response"):
            resp = QueryResponse.model_validate(body["query_response"]).model_dump(mode="json")
            await engine.on_query_response(ref, str(resp["query_id"]), resp)
            answered = True
        elif qid:
            await engine.on_query_response(ref, qid, body)
            answered = True
        return {"claim_ref": ref, "accepted": len(body.get("documents", [])), "query_answered": answered}

    @app.post("/v1/hospital-api/claims/{ref}/query-responses", status_code=202, dependencies=[Depends(chaos_gate)])
    async def query_response(ref: str, request: Request) -> dict[str, Any]:
        await _claim(ref)
        try:
            resp = QueryResponse.model_validate_json(request.state.raw_body).model_dump(mode="json")
        except ValidationError as exc:
            raise ProblemError("validation_error", "request body failed validation") from exc
        q = await store.get_query(str(resp["query_id"]))
        if q is None or q["claim_ref"] != ref:
            raise ProblemError("not_found", "unknown query", status=404)
        await engine.on_query_response(ref, str(resp["query_id"]), resp)
        return {"query_id": resp["query_id"], "status": "answered"}

    @app.post("/v1/hospital-api/claims/{ref}/withdraw", dependencies=[Depends(chaos_gate)])
    async def withdraw(ref: str, request: Request) -> dict[str, Any]:
        c = await _claim(ref)
        try:
            WithdrawRequest.model_validate_json(request.state.raw_body)
        except ValidationError as exc:
            raise ProblemError("validation_error", "request body failed validation") from exc
        if c["state"] in ("approved", "partially_approved", "rejected", "settled", "closed") or c["closed"] and not c["withdrawn"]:
            raise ProblemError("invalid_transition", f"cannot withdraw a claim that is {c['state']}", status=409)
        await engine.on_withdraw(ref)
        return {"claim_ref": ref, "status": "closed"}

    # ------------------------------------------------------------------ minimal UI (server-rendered, no JS build)
    from html import escape

    def _page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px}}</style>{body}")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def ui_index() -> HTMLResponse:
        rows = "".join(f"<tr><td><a href='/ui/claims/{escape(c['claim_ref'])}'>{escape(c['claim_ref'])}</a></td><td>{escape(c['insurer_claim_no'])}</td><td>{escape(c['scenario_id'])}</td><td>{escape(c['state'])}</td><td>{c['sequence']}</td></tr>" for c in await store.list_claims())
        ch = engine.chaos.snapshot()
        return _page("tpa-sim", f"<h1>tpa-sim</h1><p>scenarios: {len(scenarios)} - chaos: {escape(json.dumps(ch))}</p><table><tr><th>claim</th><th>insurer no</th><th>scenario</th><th>state</th><th>seq</th></tr>{rows}</table>")

    @app.get("/ui/claims/{ref}", response_class=HTMLResponse, include_in_schema=False)
    async def ui_claim(ref: str) -> HTMLResponse:
        c = await _claim(ref)
        ev = "".join(f"<tr><td>{e['id']}</td><td>{escape(str(e['step_id']))}</td><td>{escape(e['kind'])}</td><td>{escape(e['due_at'])}</td><td>{escape(str(e['result']))}</td></tr>" for e in await store.events_for(ref))
        return _page(ref, f"<h1>{escape(ref)}</h1><p>{escape(c['scenario_id'])} - {escape(c['state'])} - paused={c['paused']}</p><table><tr><th>#</th><th>step</th><th>kind</th><th>due</th><th>result</th></tr>{ev}</table>")

    app.include_router(bank_router(bank))

    # ------------------------------------------------------------------ control plane
    @app.get("/sim/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "scenarios": len(scenarios), "now": clock.now().isoformat()}

    @app.get("/sim/scenarios")
    async def list_scenarios() -> list[dict[str, Any]]:
        return [{"id": s.id, "name": s.name, "description": s.description, "steps": len(s.steps)} for s in scenarios.values()]

    @app.get("/sim/claims")
    async def sim_claims() -> list[dict[str, Any]]:
        return [{k: c[k] for k in ("claim_ref", "insurer_claim_no", "scenario_id", "state", "sequence", "step_cursor", "paused", "withdrawn", "closed")} for c in await store.list_claims()]

    @app.get("/sim/claims/{ref}")
    async def sim_claim(ref: str) -> dict[str, Any]:
        c = await _claim(ref)
        return {**{k: v for k, v in c.items() if k != "payload"}, "events": await store.events_for(ref), "queries": await store.queries_for(ref)}

    @app.post("/sim/tick")
    async def tick() -> dict[str, int]:
        return {"fired": await engine.tick()}

    @app.post("/sim/claims/{ref}/pause")
    async def pause(ref: str) -> dict[str, bool]:
        await _claim(ref)
        await store.update_claim(ref, paused=True)
        return {"paused": True}

    @app.post("/sim/claims/{ref}/resume")
    async def resume(ref: str) -> dict[str, bool]:
        await _claim(ref)
        await store.update_claim(ref, paused=False)
        return {"paused": False}

    @app.post("/sim/claims/{ref}/fire-next")
    async def fire_next(ref: str) -> dict[str, Any]:
        await _claim(ref)
        pend = [e for e in await store.events_for(ref) if e["fired_at"] is None and not e["cancelled"]]
        if not pend:
            return {"fired": None}
        return {"fired": pend[0]["id"], "result": await engine.fire(pend[0])}

    @app.get("/sim/log")
    async def log(claim_ref: str | None = None, direction: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return await store.read_log(claim_ref, direction, limit)

    @app.post("/sim/reset")
    async def reset() -> dict[str, bool]:
        await store.reset()
        engine.chaos.clear()
        bank.payouts.clear()
        bank.ledger_rows.clear()
        return {"ok": True}

    @app.post("/sim/chaos")
    async def set_chaos(request: Request) -> dict[str, Any]:
        try:
            engine.chaos.update(**json.loads(await request.body() or b"{}"))
        except (ValueError, KeyError) as exc:
            raise ProblemError("validation_error", str(exc)) from exc
        return engine.chaos.snapshot()

    @app.delete("/sim/chaos")
    async def clear_chaos() -> dict[str, Any]:
        engine.chaos.clear()
        return engine.chaos.snapshot()

    @app.get("/sim/chaos")
    async def get_chaos() -> dict[str, Any]:
        return engine.chaos.snapshot()

    @app.post("/sim/scenarios")
    async def put_scenario(request: Request) -> dict[str, str]:
        try:
            sc = dsl.parse_scenario((await request.body()).decode())
        except Exception as exc:  # noqa: BLE001
            raise ProblemError("validation_error", f"invalid scenario: {exc}") from exc
        scenarios[sc.id] = sc
        await store.upsert_scenario(sc.id, sc.name, sc.description, [s.model_dump() for s in sc.steps], sc.match, "custom")
        return {"id": sc.id}

    app.add_middleware(
        ContractAuthMiddleware,
        protected_prefixes=["/v1/hospital-api/", "/bank/", "/preauth/"],
        secrets=lambda k: [cfg.bank_secret.encode()] if k == BANK_KEY_ID else ([s.encode() for s in [cfg.hosp_keys[k]]] if k in cfg.hosp_keys else None),
        idempotency=MemoryIdempotencyStore(),
        rate_limiter=MemoryRateLimiter(cfg.rate_limit),
    )
    return app
