from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from docpipe.llm import LLMUnavailable
from docpipe.stages.render import ParseError


class Jobs:
    def __init__(
        self,
        fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        workers: int = 2,
        ttl_s: int = 3600,
    ) -> None:
        self.fn, self.sem, self.ttl = fn, asyncio.Semaphore(workers), ttl_s
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def submit(self, body: dict[str, Any]) -> str:
        now = time.time()
        for k in [
            k for k, j in self.jobs.items() if j.get("finished") and now - j["finished"] > self.ttl
        ]:
            del self.jobs[k]
        jid = f"pj-{uuid.uuid4().hex[:12]}"
        self.jobs[jid] = {"job_id": jid, "state": "queued"}
        t = asyncio.create_task(self._run(jid, body))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return jid

    async def _run(self, jid: str, body: dict[str, Any]) -> None:
        async with self.sem:
            self.jobs[jid]["state"] = "running"
            try:
                self.jobs[jid].update(state="succeeded", result=await self.fn(body))
            except ParseError as e:
                self.jobs[jid].update(state="failed", error=e.code, detail=e.detail)
            except LLMUnavailable as e:
                self.jobs[jid].update(state="failed", error="llm_unavailable", detail=str(e))
            except Exception as e:  # noqa: BLE001
                self.jobs[jid].update(
                    state="failed", error="internal_error", detail=type(e).__name__
                )
            finally:
                self.jobs[jid]["finished"] = time.time()
