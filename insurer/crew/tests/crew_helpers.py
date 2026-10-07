from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from insurer_crew.app import create_app
from insurer_crew.runtime import LLMResult
from insurer_crew.tools import Chunk


class ScriptedLLM:
    """Offline stand-in for the gateway: ``handler(agent, messages, call_no) -> str | LLMResult | raises``."""

    def __init__(self, handler: Callable[[str, list[dict[str, str]], int], Any]) -> None:
        self.handler, self.calls = handler, []

    async def complete(self, *, alias, messages, schema, metadata, max_tokens, timeout):  # noqa: ASYNC109
        self.calls.append({"alias": alias, "agent": metadata.get("agent"), "messages": messages, "metadata": metadata, "schema": schema})
        r = self.handler(metadata.get("agent", ""), messages, len(self.calls))
        if isinstance(r, Exception):
            raise r
        if hasattr(r, "__await__"):
            r = await r
        return r if isinstance(r, LLMResult) else LLMResult(r if isinstance(r, str) else json.dumps(r), 100, 50, "scripted-model")


class FakeRag:
    def __init__(self, chunks: list[Chunk] | Exception) -> None:
        self.chunks, self.calls = chunks, []

    async def search(self, collection, query, filters, top_k=4, *, case_id=None):
        self.calls.append((collection, query, filters))
        if isinstance(self.chunks, Exception):
            raise self.chunks
        return self.chunks


def make_client(llm, settings, rag=None, **kw):
    app = create_app(settings, llm=llm, rag=rag, **kw)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://crew", headers={"X-Service-Token": "dev"}), app


def body(context: dict[str, Any], rid: str | None = None, **opts: Any) -> dict[str, Any]:
    return {"request_id": rid or str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "context": context, "options": opts}
