"""CrewAI <-> the crew's own model clients. Every CrewAI agent talks to the model through `BridgeLLM`, which forwards the
prompt to OllamaLLM, RulesLLM or a test FakeLLM. The PII guard, the circuit breaker and JSON repair therefore still sit in
front of every call CrewAI makes, and `CREW_LLM=rules` runs the same agents and flows with no model at all."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from crewai.llms.base_llm import BaseLLM
from pydantic import PrivateAttr

from crew.llm import LLM


def flatten(messages: str | list[dict[str, Any]]) -> str:
    """CrewAI sends chat messages (system: role/goal/backstory, user: the task); the clients take one prompt."""
    if isinstance(messages, str):
        return messages
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):  # multimodal parts: keep the text only
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(str(c))
    return "\n\n".join(parts)


class BridgeLLM(BaseLLM):
    llm_type: str = "hospital-bridge"
    provider: str = "ollama"
    _inner: Any = PrivateAttr(default=None)
    _loop: Any = PrivateAttr(default=None)
    _infos: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @classmethod
    def over(cls, inner: LLM, model: str) -> BridgeLLM:
        b = cls(model=model, temperature=0)
        b._inner, b._loop = inner, asyncio.get_running_loop()
        return b

    @property
    def last_info(self) -> dict[str, Any]:
        return (
            self._infos[-1]
            if self._infos
            else {"model": self.model, "tokens_in": 0, "tokens_out": 0}
        )

    @property
    def calls(self) -> int:
        return len(self._infos)

    async def _complete(self, messages: str | list[dict[str, Any]]) -> str:
        obj, info = await self._inner.complete_json(model=self.model, prompt=flatten(messages))
        self._infos.append(info)
        return json.dumps(obj, ensure_ascii=False)

    def call(self, messages: Any, tools: Any = None, callbacks: Any = None, available_functions: Any = None,
             from_task: Any = None, from_agent: Any = None, response_model: Any = None) -> str:  # fmt: skip
        # CrewAI's executor calls the sync API from a worker thread; the clients are async and bound to the job's loop.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise RuntimeError("BridgeLLM.call on the event loop thread would deadlock; use acall")
        return asyncio.run_coroutine_threadsafe(self._complete(messages), self._loop).result()

    async def acall(self, messages: Any, tools: Any = None, callbacks: Any = None, available_functions: Any = None,
                    from_task: Any = None, from_agent: Any = None, response_model: Any = None) -> str:  # fmt: skip
        return await self._complete(messages)

    def supports_function_calling(self) -> bool:
        return False  # agents here have no tools: deterministic tools run in the flow, not via model tool calls

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192
