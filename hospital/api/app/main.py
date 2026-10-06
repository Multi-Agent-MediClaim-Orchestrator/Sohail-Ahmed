"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.deps import AuthSessionMiddleware, assert_all_routes_guarded
from app.auth.jwks import JwksCache
from app.core.config import Settings, get_settings
from app.core.errors import install_handlers
from app.core.logging import RequestIdMiddleware, setup_logging
from app.events import InMemoryHub, noop_completeness
from app.routers import admin_users, health, me
from app.services.n8n import HttpN8n
from app.storage.clamav import ClamAV
from app.storage.minio import ObjectStore

CODE_HEAD = "0018"


def create_app(
    settings: Settings | None = None,
    *,
    hub: Any = None,
    jwks_client: httpx.AsyncClient | None = None,
    extra_routers: list[Any] | None = None,
    **services: Any,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(s.log_level)
        app.state.engine = create_async_engine(
            s.db_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
            connect_args={"server_settings": {"timezone": "UTC"}},
        )
        app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
        app.state.redis = services.get("redis") or aioredis.from_url(
            s.redis_url, decode_responses=True
        )
        app.state.jwks = JwksCache(
            s.oidc_jwks_url, s.jwks_ttl_s, s.jwks_stale_max_s, client=jwks_client
        )
        await app.state.jwks.prefetch()
        app.state.settings = s
        app.state.hub = hub or InMemoryHub()
        app.state.code_head = CODE_HEAD
        app.state.completeness = services.get("completeness", noop_completeness)
        app.state.store = services.get("store") or ObjectStore(
            s.minio_endpoint, s.minio_access_key, s.minio_secret_key, s.minio_bucket, s.minio_secure
        )
        app.state.clam = services.get("clam") or ClamAV(s.clamav_host, s.clamav_port)
        app.state.n8n = services.get("n8n") or HttpN8n(s.n8n_webhook_base)
        for name, svc in services.items():
            setattr(app.state, name, svc)
        yield
        await app.state.engine.dispose()
        if "redis" not in services:
            await app.state.redis.aclose()

    app = FastAPI(title="Hospital API", version="1.0.0", lifespan=lifespan)
    install_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=["X-Request-ID", "Location", "ETag"],
    )
    app.add_middleware(AuthSessionMiddleware)
    app.add_middleware(RequestIdMiddleware)
    from app.routers import registry  # noqa: PLC0415  (later steps register their routers here)

    app.state.api_routers = [
        health.router,
        me.router,
        admin_users.router,
        *registry.routers(),
        *(extra_routers or []),
    ]
    for r in app.state.api_routers:
        app.include_router(r)
    return app


def create_checked_app(settings: Settings | None = None, **kw: Any) -> FastAPI:
    app = create_app(settings, **kw)
    assert_all_routes_guarded(app)
    return app
