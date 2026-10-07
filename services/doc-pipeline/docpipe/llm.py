from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


class LLMUnavailable(Exception):
    pass


class LLM(Protocol):
    async def json(self, model: str, prompt: str) -> dict[str, Any]: ...


class Ollama:
    def __init__(
        self, base: str, timeout: float = 180.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self.base, self.c = base.rstrip("/"), client or httpx.AsyncClient(timeout=timeout)

    async def json(self, model: str, prompt: str) -> dict[str, Any]:
        msgs = [{"role": "user", "content": prompt}]
        for _ in range(3):
            try:
                r = await self.c.post(
                    f"{self.base}/chat/completions",
                    json={
                        "model": model,
                        "messages": msgs,
                        "temperature": 0,
                        "stream": False,
                        "response_format": {"type": "json_object"},
                    },
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMUnavailable(type(e).__name__) from e
            content = r.json()["choices"][0]["message"]["content"] or ""
            try:
                obj = json.loads(content)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
            msgs = [
                *msgs,
                {"role": "assistant", "content": content[:1500]},
                {"role": "user", "content": "Reply with the JSON object only."},
            ]
        raise LLMUnavailable("invalid_json")


class Fake:
    """Test double. `script[model]` is a dict (or list consumed in order) returned for that model alias."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script, self.calls = script, []

    async def json(self, model: str, prompt: str) -> dict[str, Any]:
        self.calls.append((model, prompt))
        r = self.script.get(model)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if r is None or isinstance(r, Exception):
            raise (r if isinstance(r, Exception) else LLMUnavailable("no script"))
        return r  # type: ignore[no-any-return]
