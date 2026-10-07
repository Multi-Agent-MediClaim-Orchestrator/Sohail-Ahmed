"""OpenAI-compatible client for Ollama (local server; `*-cloud` models are proxied by Ollama itself).
JSON-only answers, validated by the caller; up to 2 repair retries with the error fed back."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

import httpx

from crew.guards.pii import assert_no_pii


class LLMUnavailable(Exception):
    pass


class SchemaInvalid(Exception):
    pass


class LLM(Protocol):
    async def complete_json(
        self, *, model: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


class OllamaLLM:
    def __init__(
        self, base_url: str, timeout: float = 120.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.fail_times: list[float] = []

    def _breaker_open(self) -> bool:
        now = time.monotonic()
        self.fail_times = [t for t in self.fail_times if now - t < 60]
        return len(self.fail_times) >= 5 and now - self.fail_times[-1] < 30

    async def complete_json(
        self, *, model: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert_no_pii(prompt)  # raises PiiDetected before any network call
        if self._breaker_open():
            raise LLMUnavailable("circuit open")
        messages = [{"role": "user", "content": prompt}]
        last_err = ""
        for _attempt in range(3):
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "stream": False,
                                    "response_format": {"type": "json_object"}, "reasoning_effort": "none"}  # fmt: skip
            try:
                r = await self.client.post(f"{self.base}/chat/completions", json=body)
                r.raise_for_status()
            except httpx.HTTPError as e:
                self.fail_times.append(time.monotonic())
                raise LLMUnavailable(type(e).__name__) from e
            data = r.json()
            content = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {})
            try:
                obj = json.loads(content)
                if not isinstance(obj, dict):
                    raise ValueError("not an object")
                return obj, {
                    "model": model,
                    "tokens_in": usage.get("prompt_tokens"),
                    "tokens_out": usage.get("completion_tokens"),
                }
            except ValueError as e:
                last_err = str(e)
                messages = [*messages, {"role": "assistant", "content": content[:2000]},
                            {"role": "user", "content": f"That was not valid JSON ({last_err}). Reply with the JSON object only."}]  # fmt: skip
        raise SchemaInvalid(last_err)


class FakeLLM:
    """Test double: `script` maps a substring of the prompt to a response (or list, consumed in order)."""

    def __init__(
        self, script: dict[str, Any] | None = None, default: dict[str, Any] | None = None
    ) -> None:
        self.script, self.default, self.calls = script or {}, default, []

    async def complete_json(
        self, *, model: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert_no_pii(prompt)
        self.calls.append((model, prompt))
        for key, resp in self.script.items():
            if key in prompt:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp, {"model": model, "tokens_in": 0, "tokens_out": 0}
        if self.default is None:
            raise LLMUnavailable("no scripted response")
        return self.default, {"model": model, "tokens_in": 0, "tokens_out": 0}


class RulesLLM:
    """Deterministic stand-in for the model (CREW_LLM=rules): category-based triage, a short grounded reply that quotes
    the insurer's own words, and no category guesses. For offline runs and the fast end-to-end test; never invents facts."""

    async def complete_json(
        self, *, model: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        info = {"model": "rules", "tokens_in": 0, "tokens_out": 0}
        if "<lines>" in prompt:  # category mapping: leave unknown lines as "other"
            return {"items": []}, info
        if "triage an insurer's query" in prompt:
            cat = re.search(r"Category: (\w+)", prompt)
            docs = (cat.group(1) if cat else "") in ("missing_document", "illegible_document")
            return {
                "action": "send_documents" if docs else "clarify",
                "needs_docs": docs,
                "escalation_risk": False,
                "note": "rule-based triage",
            }, info
        if "Sources:" in prompt:  # draft_reply: S1 is the query text
            src = re.search(r"\nS1: (.+)", prompt)
            q = (src.group(1) if src else "your query").strip()
            quote = q[:60].rstrip()
            text = f'Thank you for your query: "{quote}". The details are in the documents submitted with the claim, and we are glad to clarify any point.'
            return {
                "draft_text": text,
                "citations": [{"source_id": "S1", "quote": quote}],
                "missing": [],
            }, info
        return {}, info
