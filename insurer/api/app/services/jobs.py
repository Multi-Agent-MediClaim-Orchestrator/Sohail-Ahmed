"""Background job abstraction. ``manual`` (tests) collects jobs until ``drain()``; ``inline`` runs them as asyncio tasks;
``arq`` enqueues to Redis for ``app/worker.py``. Handlers are plain ``async def fn(**kwargs)`` — idempotent by design."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("jobs")
Handler = Callable[..., Awaitable[Any]]

_handlers: dict[str, Handler] = {}
_queue: list[tuple[str, dict[str, Any]]] = []
_tasks: set[asyncio.Task[Any]] = set()
mode = "manual"
_arq_pool: Any = None


def register(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[name] = fn
        return fn

    return deco


def configure(new_mode: str, arq_pool: Any = None) -> None:
    global mode, _arq_pool
    mode, _arq_pool = new_mode, arq_pool


async def enqueue(name: str, **kwargs: Any) -> None:
    if mode == "arq" and _arq_pool is not None:
        await _arq_pool.enqueue_job(name, **kwargs)
    elif mode == "inline":
        t = asyncio.create_task(_run(name, kwargs))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    else:
        _queue.append((name, kwargs))


async def _run(name: str, kwargs: dict[str, Any]) -> Any:
    try:
        return await _handlers[name](**kwargs)
    except Exception:  # job failures are logged, never raised into request handlers
        log.exception("job %s failed", name)
        return None


def pending() -> list[tuple[str, dict[str, Any]]]:
    return list(_queue)


async def drain(only: set[str] | None = None, *, max_rounds: int = 20) -> int:
    """Run queued jobs (including ones they enqueue) until the queue is empty. Returns the number executed."""
    ran = 0
    for _ in range(max_rounds):
        batch = [j for j in _queue if only is None or j[0] in only]
        if not batch:
            break
        for j in batch:
            _queue.remove(j)
        for name, kw in batch:
            await _run(name, kw)
            ran += 1
    return ran


def clear() -> None:
    _queue.clear()
