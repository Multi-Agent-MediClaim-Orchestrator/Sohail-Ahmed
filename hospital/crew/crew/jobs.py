"""Async job runner: queued -> running -> succeeded | failed(code, retryable) | cancelled. In-memory store with a
TTL (jobs are minutes long and the API/n8n retry anything lost on a restart)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from crew.guards.pii import PiiDetected
from crew.llm import LLMUnavailable, SchemaInvalid


@dataclass
class Job:
    id: str
    type: str
    input: dict[str, Any]
    state: str = "queued"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def envelope(self) -> dict[str, Any]:
        return {"job_id": self.id, "type": self.type, "state": self.state, "result": self.result, "error": self.error,
                "created_at": self.created_at, "finished_at": self.finished_at,
                "case_id": self.input.get("case_id"), "correlation_id": self.input.get("correlation_id")}  # fmt: skip


Handler = Callable[[Job], Awaitable[dict[str, Any]]]


class Runner:
    def __init__(
        self, handlers: dict[str, Handler], concurrency: int = 3, ttl_s: int = 86400
    ) -> None:
        self.handlers, self.ttl = handlers, ttl_s
        self.jobs: dict[str, Job] = {}
        self.sem = asyncio.Semaphore(concurrency)
        self.running = 0
        self.max_running = 0
        self.tasks: set[asyncio.Task[None]] = set()

    def submit(self, type_: str, body: dict[str, Any]) -> Job:
        self._expire()
        job = Job(str(uuid.uuid4()) if not body.get("job_id") else str(body["job_id"]), type_, body)
        self.jobs[job.id] = job
        t = asyncio.create_task(self._run(job))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return job

    def _expire(self) -> None:
        now = time.time()
        for k in [
            k for k, j in self.jobs.items() if j.finished_at and now - j.finished_at > self.ttl
        ]:
            del self.jobs[k]

    async def _run(self, job: Job) -> None:
        async with self.sem:
            if job.cancel.is_set():
                job.state, job.finished_at = "cancelled", time.time()
                return
            job.state = "running"
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            try:
                job.result = await self.handlers[job.type](job)
                job.state = "cancelled" if job.cancel.is_set() else "succeeded"
            except PiiDetected as e:
                self._fail(job, "pii_detected", str(e), False)
            except (ValidationError, SchemaInvalid) as e:
                self._fail(job, "schema_invalid", str(e)[:500], False)
            except LLMUnavailable as e:
                self._fail(job, "llm_unavailable", str(e), True)
            except TimeoutError:
                self._fail(job, "timeout", "job timed out", True)
            except Exception as e:  # noqa: BLE001
                self._fail(job, "internal_error", f"{type(e).__name__}: {e}"[:500], True)
            finally:
                self.running -= 1
                job.finished_at = time.time()

    @staticmethod
    def _fail(job: Job, code: str, detail: str, retryable: bool) -> None:
        job.state, job.error = "failed", {"code": code, "detail": detail, "retryable": retryable}

    def cancel(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j:
            return False
        j.cancel.set()
        return True
