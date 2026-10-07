"""insurer-api application factory (port 8100)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from claim_contract import CONTRACT_VERSION, SUPPORTED_VERSIONS
from claim_contract.errors import install_handlers
from claim_contract.insurer_side.idempotency import (
    ChainedIdempotencyStore,
    RedisIdempotencyStore,
    SqlIdempotencyStore,
)
from claim_contract.insurer_side.middleware import (
    ContractAuthMiddleware,
    MemoryRateLimiter,
    RedisRateLimiter,
)
from fastapi import FastAPI
from sqlalchemy import text

from . import db
from .config.service import ConfigService
from .services import events
from .settings import Settings, get_settings

log = logging.getLogger("insurer-api")


async def _connect_redis(url: str) -> Any | None:
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=2)
        await r.ping()
        return r
    except Exception:
        log.warning("redis unavailable; running in degraded mode (in-process bus, db idempotency)")
        return None


class _LazyRedis:
    """Resolves the real client at call time so middleware built before startup still uses Redis once connected."""

    def __init__(self) -> None:
        self.client: Any | None = None

    def __getattr__(self, name: str) -> Any:
        if self.client is None:
            raise ConnectionError("redis not connected")
        return getattr(self.client, name)


class _AutoLimiter:
    """Redis token window when Redis is up, in-process window otherwise (never fail open silently on a single node)."""

    def __init__(self, redis_limiter: RedisRateLimiter, mem: MemoryRateLimiter, lazy: _LazyRedis) -> None:
        self.r, self.m, self.lazy = redis_limiter, mem, lazy

    async def hit(self, key_id: str) -> tuple[bool, int, int, int]:
        return await (self.r.hit(key_id) if self.lazy.client is not None else self.m.hit(key_id))


async def _cron_loop(stop: asyncio.Event) -> None:  # pragma: no cover - long running loop
    """Single-process stand-in for the Arq cron entries: SLA tick every 5 minutes, settlement auto-close hourly."""
    from . import jobs_cron
    from .services import settlement

    n = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=300)
        except TimeoutError:
            n += 1
            try:
                await jobs_cron.sla_tick()
                if n % 12 == 0:
                    await settlement.autoclose()
            except Exception:
                log.exception("cron pass failed")


def create_app(settings: Settings | None = None, *, redis: Any | None = None, use_redis: bool = True) -> FastAPI:
    s = settings or get_settings()
    lazy = _LazyRedis()
    lazy.client = redis

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = redis if redis is not None else (await _connect_redis(s.redis_url) if use_redis else None)
        lazy.client = r
        app.state.redis = r
        if r is not None:
            events.set_bus(events.RedisEventBus(r, s.events_stream, 10_000))
        from . import jobs_cron
        from .services import (  # noqa: F401  (imports register the job handlers)
            docs_fetch,
            jobs,
            queries,
            settlement,
        )

        stop = asyncio.Event()
        bg: list[asyncio.Task[Any]] = []
        if s.jobs_mode != "manual":  # arq mode: `arq insurer_app.worker.WorkerSettings` runs the same handlers
            jobs.configure(s.jobs_mode)
        if s.run_dispatcher:  # callbacks to hospitals, parked n8n triggers, due settlement retries
            bg.append(asyncio.create_task(jobs_cron.run_dispatcher(stop)))
        if s.run_cron:
            bg.append(asyncio.create_task(_cron_loop(stop)))
        yield
        stop.set()
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        if r is not None and redis is None:
            await r.aclose()
        await db.dispose_db()

    app = FastAPI(title="insurer-api", version="1.1.0", lifespan=lifespan)
    install_handlers(app)
    idem = ChainedIdempotencyStore(
        RedisIdempotencyStore(lazy), SqlIdempotencyStore(lambda: db.sessionmaker()(), "ops.idempotency_record"), on_degraded=lambda m: log.debug(m)
    )
    limiter = _AutoLimiter(RedisRateLimiter(lazy, s.rate_limit_per_min), MemoryRateLimiter(s.rate_limit_per_min), lazy)
    app.state.config = ConfigService(lazy)
    from .services import orchestrator

    orchestrator.configure(app.state.config)
    app.state.idem = idem

    async def secrets(key_id: str) -> list[bytes] | None:
        return s.secrets_for(key_id)

    app.add_middleware(
        ContractAuthMiddleware, protected_prefixes=["/v1/hospital-api/", "/internal/settlement/bank-callback"], secrets=secrets, idempotency=idem,
        rate_limiter=limiter, skew_seconds=s.clock_skew_seconds, server_version=CONTRACT_VERSION,
    )

    from .routers import admin as admin_router
    from .routers import decisions, hospital_api, internal, reviewer
    from .routers import events as events_router
    from .routers import queries as queries_router
    from .routers import settlement as settlement_router
    from .services import decision as _decision  # noqa: F401  (registers the auto-approval hook)
    from .services import queries as _queries  # noqa: F401  (registers the query-loop hooks)
    from .services import settlement as _settlement  # noqa: F401  (registers the settlement job)

    app.include_router(hospital_api.router)
    app.include_router(reviewer.router)
    app.include_router(decisions.router)
    app.include_router(internal.router)
    app.include_router(events_router.router)
    app.include_router(queries_router.router)
    app.include_router(queries_router.internal)
    app.include_router(settlement_router.router)
    app.include_router(admin_router.router)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        try:
            async with db.sessionmaker()() as sess:
                await sess.execute(text("SELECT 1"))
        except Exception as exc:
            from claim_contract.errors import ProblemError

            raise ProblemError("service_unavailable", "database unavailable", headers={"Retry-After": "5"}) from exc
        return {"status": "ok", "version": "1.1.0", "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}

    @app.get("/v1/ready")
    async def ready() -> Any:
        """Readiness (05-04 §8): required deps -> 503 when down; optional deps (crew, rag) -> 'degraded' but still 200."""
        from fastapi.responses import JSONResponse

        from . import metrics
        from .settings import get_settings

        st = get_settings()
        deps: dict[str, str] = {}
        try:
            async with db.sessionmaker()() as sess:
                await sess.execute(text("SELECT 1"))
            deps["db"] = "ok"
        except Exception:
            deps["db"] = "down"
        if st.redis_url and use_redis:
            deps["redis"] = "ok" if getattr(app.state, "redis", None) is not None else "down"
        for name, url in (("crew", st.crew_url), ("rag", st.rag_url)):
            if url:
                deps[name] = "unchecked"
        required_down = deps.get("db") != "ok"  # redis down degrades (in-memory idempotency/rate-limit fallback) but does not stop the service
        status = "not_ready" if required_down else ("degraded" if "down" in deps.values() else "ready")
        metrics.READY.set(0 if required_down else 1)
        return JSONResponse({"status": status, "version": "1.1.0", "deps": deps}, status_code=503 if required_down else 200)

    @app.get("/v1/contract")
    async def contract() -> dict[str, Any]:
        return {"supported": SUPPORTED_VERSIONS, "default": CONTRACT_VERSION, "deprecated": []}

    return app
