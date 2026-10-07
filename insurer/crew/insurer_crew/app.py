"""insurer-crew FastAPI app: seven stateless agent endpoints (03-08 §4). No write tools, no provider keys, no confidence field."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx
import jwt
from claim_contract.errors import ProblemError, install_handlers
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import ValidationError

from . import validators
from .agents import (
    SPECS,
    AgentInvalidOutput,
    AgentSpec,
    ContextInvalid,
    Deps,
    Runner,
    alias_for,
)
from .runtime import (
    Busy,
    CacheConflict,
    ConcurrencyGate,
    LLMBudgetExceeded,
    LLMUnavailable,
    MemoryCache,
    PromptRegistry,
    SafeCache,
    Settings,
    Tracer,
    body_hash,
    get_settings,
)
from .schemas import ALL_OUTPUTS, AgentRequest, TokenUsage

ALLOWED_SERVICES = {"svc-insurer-api", "svc-n8n-insurer", "svc-eval"}


def schema_sha(name: str) -> str:
    return hashlib.sha256(json.dumps(ALL_OUTPUTS[name].model_json_schema(), sort_keys=True).encode()).hexdigest()


def create_app(settings: Settings | None = None, *, llm: Any, rag: Any = None, cache: Any = None, tracer: Tracer | None = None, prompts: PromptRegistry | None = None) -> FastAPI:
    cfg = settings or get_settings()
    deps = Deps(llm=llm, settings=cfg, prompts=prompts or PromptRegistry(), rag=rag)
    cache = SafeCache(cache or MemoryCache(cfg.crew_cache_ttl))
    gate = ConcurrencyGate(cfg.crew_max_concurrency, cfg.crew_queue_depth)
    tracer = tracer or Tracer(cfg)
    app = FastAPI(title="insurer-crew", version="1.0")
    install_handlers(app)
    app.state.deps, app.state.gate, app.state.tracer = deps, gate, tracer

    reg = CollectorRegistry()
    m_req = Counter("crew_requests_total", "agent requests", ["agent", "status"], registry=reg)
    m_repair = Counter("crew_repair_total", "JSON repair attempts", ["agent"], registry=reg)
    m_pii_out = Counter("crew_pii_output_total", "outputs with PII redacted", registry=reg)
    m_lat = Histogram("crew_latency_seconds", "agent latency", ["agent"], registry=reg, buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 90))
    m_degraded = Counter("crew_degraded_total", "degraded outputs", ["agent"], registry=reg)

    tokens = {t.strip() for t in cfg.crew_service_tokens.split(",") if t.strip()}
    if cfg.crew_allow_dev_token:
        tokens.add("dev")

    def auth(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)) -> str:
        if x_service_token and x_service_token in tokens:
            return "svc-static"
        cand = x_service_token or (authorization.split(None, 1)[1] if authorization and authorization.lower().startswith("bearer ") else None)
        if cand:
            try:
                c = jwt.decode(cand, cfg.crew_jwt_secret, algorithms=["HS256"], options={"require": ["exp", "svc"]})
                if c["svc"] in ALLOWED_SERVICES:
                    return str(c["svc"])
            except jwt.PyJWTError:
                pass
        raise ProblemError("forbidden", "invalid or missing service token", status=401)

    async def execute(spec: AgentSpec, req: AgentRequest, svc: str) -> dict[str, Any]:
        rid, bh = str(req.request_id), body_hash(req.model_dump(mode="json", exclude={"request_id"}))
        try:
            hit = await cache.get(rid, bh)
        except CacheConflict as e:
            raise ProblemError("idempotency_conflict", "request_id was already used with a different body") from e
        if hit is not None:
            return json.loads(hit)
        try:
            ctx = spec.context_model.model_validate(req.context)
        except ValidationError as e:
            raise ProblemError("context_invalid", "context failed validation", status=422, errors=[{"field": ".".join(str(p) for p in x["loc"]), "message": x["msg"]} for x in e.errors()[:20]]) from e
        pii = validators.scan_input(ctx.model_dump(mode="json"))
        if pii:
            raise ProblemError("pii_in_context", f"PII patterns in fields: {sorted(pii)[:10]}", status=400)  # field paths only
        flagged = any(validators.injection_suspected(t) for _, t in validators.iter_strings(ctx.model_dump(mode="json")))
        timeout = req.options.timeout_s or cfg.crew_request_timeout
        trace = tracer.start(str(req.case_id), rid, spec.name)
        runner = Runner(spec.name, deps, trace, alias_for(spec, cfg, req.options.model_alias if svc == "svc-eval" else None), max_tokens=req.options.max_tokens or 1024,
                        timeout=min(60.0, timeout))
        if flagged:
            runner.warnings.append("injection_suspected_input")
        t0 = time.perf_counter()
        try:
            async with gate.slot():
                fields = await asyncio.wait_for(spec.fn(ctx, runner), timeout=timeout)
        except Busy as e:
            raise ProblemError("busy", "crew is saturated", status=429, headers={"Retry-After": "5"}) from e
        except TimeoutError as e:
            raise ProblemError("timeout", "agent timed out", status=504) from e
        except LLMBudgetExceeded as e:
            raise ProblemError("budget_exceeded", "LLM token budget exhausted", status=429) from e
        except LLMUnavailable as e:
            raise ProblemError("llm_unavailable", str(e), status=503, headers={"Retry-After": "30"}) from e
        except AgentInvalidOutput as e:
            raise ProblemError("agent_invalid_output", "model output failed validation after repair", status=422, errors=[{"field": ".".join(str(p) for p in x.get("loc", ())), "message": str(x.get("msg"))} for x in (e.errors or [])[:10]]) from e
        except ContextInvalid as e:
            raise ProblemError("context_invalid", str(e), status=422) from e
        finally:
            m_lat.labels(spec.name).observe(time.perf_counter() - t0)
            m_repair.labels(spec.name).inc(trace.repairs)
        if runner.degraded and not req.options.allow_degraded:
            raise ProblemError("llm_unavailable", "only a degraded model was available", status=503)
        pv = deps.prompts.get(spec.prompt)[1]
        meta = {"trace_id": trace.id, "prompt_version": pv, "model_alias": runner.served_alias or runner.alias,
                "token_usage": TokenUsage(prompt=runner.usage_prompt, completion=runner.usage_completion, total=runner.usage_prompt + runner.usage_completion).model_dump(),
                "degraded": runner.degraded, "warnings": list(runner.warnings)}
        flds = dict(fields)
        extra_warn = flds.pop("warnings", [])
        out = ALL_OUTPUTS[spec.name].model_validate({**flds, **meta, "warnings": list(runner.warnings) + list(extra_warn)})
        out, pii_warn = validators.redact_model(out)  # output PII scan: redact + warn (counted)
        if pii_warn:
            m_pii_out.inc(len(pii_warn))
            out.warnings = list(out.warnings) + pii_warn  # type: ignore[attr-defined]
        if runner.degraded:
            m_degraded.labels(spec.name).inc()
        body = out.model_dump(mode="json")
        await cache.put(rid, bh, json.dumps(body))
        await tracer.flush(trace)
        return body

    def make_endpoint(spec: AgentSpec):
        async def endpoint(req: AgentRequest, svc: str = Depends(auth)) -> JSONResponse:
            try:
                body = await execute(spec, req, svc)
            except ProblemError as e:
                m_req.labels(spec.name, str(e.status)).inc()
                raise
            m_req.labels(spec.name, "200").inc()
            return JSONResponse(body)

        return endpoint

    for spec in SPECS.values():
        app.add_api_route(spec.path, make_endpoint(spec), methods=["POST"], name=spec.name)

    @app.get("/v1/agents")
    async def agents(_: str = Depends(auth)) -> list[dict[str, Any]]:
        return [{"agent": s.name, "endpoint": s.path, "alias": alias_for(s, cfg), "prompt_version": deps.prompts.get(s.prompt)[1], "schema_sha256": schema_sha(s.name), "last_eval_report": None} for s in SPECS.values()]

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "gateway": None, "rag": None}
        async with httpx.AsyncClient(timeout=2) as c:
            for name, url, path in (("gateway", cfg.llm_gateway_url, "/health/liveliness"), ("rag", cfg.rag_url, "/health")):
                if not url:
                    continue
                try:
                    out[name] = (await c.get(url.rstrip("/") + path)).status_code == 200
                except httpx.HTTPError:
                    out[name] = False
        return out

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(reg), media_type="text/plain; version=0.0.4")

    return app
