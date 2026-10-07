"""Config, LLM client, concurrency gate, idempotency cache, tracing, prompt registry (03-08 §5 tasks 2, 4, 5, 10-13)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

PROMPT_DIR = Path(__file__).parent / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INS_", env_file=".env", extra="ignore")

    llm_gateway_url: str = "http://llm-gateway:4000"
    llm_virtual_key: str = ""
    rag_url: str = ""
    rag_token: str = ""
    presidio_url: str = ""
    langfuse_host: str = ""
    langfuse_public: str = ""
    langfuse_secret: str = ""
    crew_max_concurrency: int = 2
    crew_queue_depth: int = 5
    crew_request_timeout: float = 90
    crew_cache_ttl: int = 600
    crew_max_context_tokens: int = 12000
    crew_repair_retries: int = 2
    crew_jwt_secret: str = "dev-crew-jwt-secret-0000000000000000000000"
    crew_service_tokens: str = ""  # comma list of static tokens (X-Service-Token); dev default below
    crew_allow_dev_token: bool = True
    alias_smart: str = "reason-cloud"  # ins-smart -> gateway alias (04-04 §3; the gateway key for this service allows only these two)
    alias_fast: str = "reason-cloud"  # ins-fast: same alias, the gateway's router handles free-tier limits
    llm_reasoning_effort: str = ""  # "none" for Ollama models that think by default (several times slower for no gain here)
    alias_fallback: str = "reason-local"  # served by the gateway when the cloud alias fails; surfaced as degraded=true


def get_settings() -> Settings:
    return Settings()


# ------------------------------------------------------------------------------------------------ LLM
@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    served_by: str = ""
    degraded: bool = False


class LLMUnavailable(Exception):
    pass


class LLMBudgetExceeded(Exception):
    pass


class LLM(Protocol):
    async def complete(self, *, alias: str, messages: list[dict[str, str]], schema: dict[str, Any] | None, metadata: dict[str, Any], max_tokens: int, timeout: float) -> LLMResult: ...  # noqa: ASYNC109


class GatewayLLM:
    """OpenAI-compatible calls to llm-gateway. This container holds only the gateway virtual key - never a provider key."""

    def __init__(self, base_url: str, key: str, transport: httpx.AsyncBaseTransport | None = None, reasoning_effort: str = "") -> None:
        self.reasoning_effort = reasoning_effort
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, transport=transport)

    async def complete(self, *, alias: str, messages: list[dict[str, str]], schema: dict[str, Any] | None, metadata: dict[str, Any], max_tokens: int, timeout: float) -> LLMResult:  # noqa: ASYNC109
        body: dict[str, Any] = {"model": alias, "messages": messages, "temperature": 0, "max_tokens": max_tokens, "metadata": {"system": "insurer", **metadata}}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": metadata.get("agent", "out"), "schema": schema, "strict": False}}
        try:
            r = await self.http.post("/v1/chat/completions", json=body, timeout=timeout)
        except httpx.TimeoutException as e:
            raise LLMUnavailable("timeout") from e
        except httpx.HTTPError as e:
            raise LLMUnavailable(str(e)) from e
        if r.status_code == 429 and "budget_exceeded" in r.text:
            raise LLMBudgetExceeded
        if r.status_code >= 400:
            raise LLMUnavailable(f"gateway {r.status_code}")
        j = r.json()
        usage = j.get("usage") or {}
        return LLMResult(j["choices"][0]["message"]["content"] or "", int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), j.get("model", ""),
                         r.headers.get("x-llm-fallback-used") == "true")


# ------------------------------------------------------------------------------------------------ concurrency (task 10)
class Busy(Exception):
    pass


class ConcurrencyGate:
    def __init__(self, limit: int, queue_depth: int) -> None:
        self.sem = asyncio.Semaphore(limit)
        self.limit, self.queue_depth = limit, queue_depth
        self.running = 0
        self.waiting = 0

    @asynccontextmanager
    async def slot(self):
        if self.running >= self.limit and self.waiting >= self.queue_depth:
            raise Busy
        self.waiting += 1
        try:
            await self.sem.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self.sem.release()


# ------------------------------------------------------------------------------------------------ idempotency cache (task 11)
class CacheConflict(Exception):
    pass


class MemoryCache:
    def __init__(self, ttl: int = 600, now=time.monotonic) -> None:
        self.ttl, self.now = ttl, now
        self.d: dict[str, tuple[float, str, str]] = {}

    async def get(self, rid: str, body_hash: str) -> str | None:
        rec = self.d.get(rid)
        if rec is None or rec[0] < self.now():
            self.d.pop(rid, None)
            return None
        if rec[1] != body_hash:
            raise CacheConflict
        return rec[2]

    async def put(self, rid: str, body_hash: str, value: str) -> None:
        self.d[rid] = (self.now() + self.ttl, body_hash, value)


class RedisCache:
    def __init__(self, redis: Any, ttl: int = 600) -> None:
        self.r, self.ttl = redis, ttl

    async def get(self, rid: str, body_hash: str) -> str | None:
        raw = await self.r.get(f"crew:idem:{rid}")
        if raw is None:
            return None
        h, _, val = raw.partition("|")
        if h != body_hash:
            raise CacheConflict
        return val

    async def put(self, rid: str, body_hash: str, value: str) -> None:
        await self.r.set(f"crew:idem:{rid}", f"{body_hash}|{value}", ex=self.ttl)


class SafeCache:
    """Redis down -> skip the cache (task: Redis outage), never fail the request."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def get(self, rid: str, body_hash: str) -> str | None:
        try:
            return await self.inner.get(rid, body_hash)
        except CacheConflict:
            raise
        except Exception:  # noqa: BLE001
            return None

    async def put(self, rid: str, body_hash: str, value: str) -> None:
        try:
            await self.inner.put(rid, body_hash, value)
        except Exception:  # noqa: BLE001
            return


def body_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ tracing (task 13)
@dataclass
class Span:
    name: str
    start: float
    end: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self, case_id: str, request_id: str, agent: str) -> None:
        self.id = "lf-" + uuid.uuid4().hex[:16]
        self.case_id, self.request_id, self.agent = case_id, request_id, agent
        self.spans: list[Span] = []
        self.repairs = 0

    @asynccontextmanager
    async def span(self, name: str, **meta: Any):
        s = Span(name, time.perf_counter(), meta=meta)
        self.spans.append(s)
        try:
            yield s
        finally:
            s.end = time.perf_counter()


class Tracer:
    """Keeps recent traces in memory; ``flush`` posts them to Langfuse (best-effort, never raises)."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s, self.transport = settings, transport
        self.recent: list[Trace] = []

    def start(self, case_id: str, request_id: str, agent: str) -> Trace:
        t = Trace(case_id, request_id, agent)
        self.recent = (self.recent + [t])[-200:]
        return t

    async def flush(self, t: Trace) -> None:
        if not self.s.langfuse_host:
            return
        batch = [{"id": str(uuid.uuid4()), "type": "trace-create", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "body": {"id": t.id, "name": f"insurer.{t.agent}", "sessionId": t.case_id, "metadata": {"request_id": t.request_id, "repairs": t.repairs}, "userId": "system:insurer-crew"}}]
        try:
            async with httpx.AsyncClient(base_url=self.s.langfuse_host, auth=(self.s.langfuse_public, self.s.langfuse_secret), transport=self.transport, timeout=5) as c:
                await c.post("/api/public/ingestion", json={"batch": batch})
        except Exception:  # noqa: BLE001 - tracing must never fail a request
            return


# ------------------------------------------------------------------------------------------------ prompt registry (task 5)
class PromptRegistry:
    def __init__(self, directory: Path = PROMPT_DIR) -> None:
        self.dir = directory
        self._cache: dict[str, tuple[str, str]] = {}

    def get(self, name: str) -> tuple[str, str]:
        """(text, ``<name>@<sha6>``) - editing a prompt changes its version, which invalidates replay cassettes and eval reports."""
        if name not in self._cache:
            text = (self.dir / f"{name}.md").read_text(encoding="utf-8")
            self._cache[name] = (text, f"{name}@{hashlib.sha256(text.encode()).hexdigest()[:6]}")
        return self._cache[name]

    def all(self) -> dict[str, str]:
        return {p.stem: self.get(p.stem)[1] for p in sorted(self.dir.glob("*.md"))}
