"""llm-gateway client: embeddings (alias ``embed``) and structured chat (alias ``reason-cloud``). rag-service holds no provider key."""

from __future__ import annotations

import time
from typing import Any

import httpx


class GatewayUnavailable(Exception):
    pass


class GatewayEmbedder:
    dim = 768

    def __init__(self, base_url: str, key: str, alias: str = "embed", batch: int = 16, transport: httpx.BaseTransport | None = None, retries: int = 3, backoff: float = 0.5) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=120, transport=transport)
        self.alias, self.batch, self.retries, self.backoff = alias, batch, retries, backoff
        self.model_id = "unknown"

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        agent = "rag-ingest" if kind == "document" else "rag-query"
        for i in range(0, len(texts), self.batch):
            body = {"model": self.alias, "input": texts[i : i + self.batch], "metadata": {"system": "shared", "agent": agent, "prompt_version": "n/a"}}
            last: Exception | None = None
            for attempt in range(self.retries):
                try:
                    r = self.http.post("/v1/embeddings", json=body)
                    if r.status_code >= 500 or r.status_code == 429:
                        raise GatewayUnavailable(f"gateway {r.status_code}")
                    r.raise_for_status()
                    self.model_id = r.headers.get("x-embed-model", self.model_id)
                    out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
                    break
                except (httpx.HTTPError, GatewayUnavailable) as e:
                    last = e
                    time.sleep(self.backoff * (2**attempt))
            else:
                raise GatewayUnavailable(f"embedding failed after {self.retries} attempts: {last}")
        return out


class GatewayChat:
    def __init__(self, base_url: str, key: str, alias: str = "reason-cloud", transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=120, transport=transport)
        self.alias = alias

    def __call__(self, messages: list[dict[str, str]], response_format: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = {"system": "shared", "agent": "rag-answer", "prompt_version": "rag-answer@1", **(metadata or {})}
        r = self.http.post("/v1/chat/completions", json={"model": self.alias, "messages": messages, "temperature": 0, "max_tokens": 1024, "response_format": response_format, "metadata": meta})
        if r.status_code >= 400:
            raise GatewayUnavailable(f"gateway {r.status_code}: {r.text[:200]}")
        j = r.json()
        return {"content": j["choices"][0]["message"]["content"],
                "model": {"alias": self.alias, "served_by": j.get("model"), "fallback_used": r.headers.get("x-llm-fallback-used") == "true"}}
