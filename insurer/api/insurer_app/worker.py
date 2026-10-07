"""Arq worker entrypoint (03-02 §5 task 17): ``arq insurer_app.worker.WorkerSettings``.

Functions: fetch_documents, start_verification, triage_response, improve_query_draft, initiate_settlement. Cron: sla_tick,
purge_ops, settlement_autoclose; a background dispatcher loop delivers callbacks every second."""

from __future__ import annotations

import asyncio
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from . import db, jobs_cron
from .config.service import ConfigService
from .services import (  # noqa: F401  (imports register the job handlers)
    docs_fetch,
    jobs,
    orchestrator,
    queries,
    settlement,
)
from .settings import get_settings


def _wrap(name: str) -> Any:
    async def fn(ctx: dict[str, Any], **kwargs: Any) -> Any:
        return await jobs._handlers[name](**kwargs)

    fn.__name__ = name
    return fn


async def startup(ctx: dict[str, Any]) -> None:
    db.init_db()
    orchestrator.configure(ConfigService())
    ctx["stop"] = asyncio.Event()
    ctx["dispatcher"] = asyncio.create_task(jobs_cron.run_dispatcher(ctx["stop"]))


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["stop"].set()
    await ctx["dispatcher"]
    await db.dispose_db()


async def _sla(ctx: dict[str, Any]) -> int:
    return await jobs_cron.sla_tick()


async def _purge(ctx: dict[str, Any]) -> dict[str, int]:
    return await jobs_cron.purge_ops()


async def _autoclose(ctx: dict[str, Any]) -> int:
    return await settlement.autoclose()


class WorkerSettings:
    functions = [_wrap(n) for n in ("fetch_documents", "start_verification", "triage_response", "improve_query_draft", "initiate_settlement")]
    cron_jobs = [cron(_sla, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, name="sla_tick"), cron(_purge, hour={20}, minute={30}, name="purge_ops"),
                 cron(_autoclose, minute={7}, name="settlement_autoclose")]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 5
    retry_jobs = True
