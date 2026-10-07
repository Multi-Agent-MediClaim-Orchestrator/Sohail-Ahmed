"""CrewAI agents for the seven insurer agents. `Runner.ask` (agents.py) calls `ask` here: one CrewAI agent (role and goal
from config/agents.yaml, backstory = the versioned system prompt) runs one task (the user content) in a sequential
crew. The schema check is the task's guardrail: an invalid answer goes back to the agent with the problems, up to
`crew_repair_retries` times, then `AgentInvalidOutput`. Every model call goes through `GatewayBridge` to the
service's own gateway client, so usage, degraded flags, tracing and the provider-key rule are unchanged."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from crewai import Agent, Crew, Process, Task, TaskOutput
from crewai.llms.base_llm import BaseLLM
from pydantic import BaseModel, PrivateAttr, ValidationError

from . import validators

if TYPE_CHECKING:
    from .agents import Runner

AGENTS: dict[str, dict[str, str]] = yaml.safe_load((Path(__file__).parent / "config" / "agents.yaml").read_text())
REPAIR_TMPL = "Your previous reply was not valid. Return ONLY corrected JSON that satisfies the schema. Problems: {errors}"
EXPECTED = "One JSON object that satisfies the response schema, with no other text."


def plain_messages(messages: str | list[dict[str, Any]]) -> list[dict[str, str]]:
    """CrewAI adds keys (cache hints) and may send a bare string; the gateway gets plain role/content pairs."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        out.append({"role": str(m.get("role", "user")), "content": str(c or "")})
    return out


class GatewayBridge(BaseLLM):
    llm_type: str = "insurer-gateway-bridge"
    provider: str = "openai"
    _runner: Any = PrivateAttr(default=None)
    _schema: Any = PrivateAttr(default=None)
    _loop: Any = PrivateAttr(default=None)
    _calls: int = PrivateAttr(default=0)

    @classmethod
    def over(cls, runner: Runner, schema: dict[str, Any]) -> GatewayBridge:
        b = cls(model=runner.alias, temperature=0)
        b._runner, b._schema, b._loop = runner, schema, asyncio.get_running_loop()
        return b

    async def _complete(self, messages: Any) -> str:
        r = self._runner
        attempt, self._calls = self._calls, self._calls + 1
        meta = {"agent": r.agent, "prompt_version": r.prompt_versions[-1], "claim_ref": r.trace.case_id,
                "request_id": r.trace.request_id, "trace_id": r.trace.id}  # fmt: skip
        async with r.trace.span("llm", agent=r.agent, attempt=attempt):
            res = await r.deps.llm.complete(alias=r.alias, messages=plain_messages(messages), schema=self._schema, metadata=meta,
                                            max_tokens=r.max_tokens, timeout=r.timeout)  # fmt: skip
        r.usage_prompt += res.prompt_tokens
        r.usage_completion += res.completion_tokens
        r.degraded = r.degraded or res.degraded
        r.served_alias = res.served_by or r.alias
        return res.text

    def call(self, messages: Any, tools: Any = None, callbacks: Any = None, available_functions: Any = None,
             from_task: Any = None, from_agent: Any = None, response_model: Any = None) -> str:  # fmt: skip
        # CrewAI's executor calls the sync API from a worker thread; the gateway client lives on the request's loop.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise RuntimeError("GatewayBridge.call on the event loop thread would deadlock; use acall")
        return asyncio.run_coroutine_threadsafe(self._complete(messages), self._loop).result()

    async def acall(self, messages: Any, tools: Any = None, callbacks: Any = None, available_functions: Any = None,
                    from_task: Any = None, from_agent: Any = None, response_model: Any = None) -> str:  # fmt: skip
        return await self._complete(messages)

    def supports_function_calling(self) -> bool:
        return False  # facts are precomputed by code (agents.py); agents do not call tools

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 16384


def build_agent(name: str, system_prompt: str, llm: BaseLLM) -> Agent:
    spec = AGENTS[name]
    return Agent(role=spec["role"], goal=spec["goal"], backstory=system_prompt, llm=llm, allow_delegation=False,
                 max_iter=2, max_retry_limit=0, verbose=False)  # fmt: skip


async def ask(runner: Runner, core: type[BaseModel], system_prompt: str, user: str) -> BaseModel:
    retries = runner.deps.settings.crew_repair_retries
    bridge = GatewayBridge.over(runner, core.model_json_schema())
    agent = build_agent(runner.agent, system_prompt, bridge)
    state: dict[str, Any] = {"attempts": 0}

    def check(out: TaskOutput):  # noqa: ANN202 - CrewAI rejects a string return annotation here
        state["attempts"] += 1
        try:
            state["value"] = core.model_validate(validators.parse_json(out.raw))
            return True, out
        except (ValidationError, ValueError) as e:
            errs = e.errors() if isinstance(e, ValidationError) else [{"msg": str(e)}]
            if state["attempts"] > retries:
                state["errors"] = errs
                return True, out  # stop CrewAI's retries; the failure is raised below with the schema errors
            runner.trace.repairs += 1
            compact = "; ".join(f"{'.'.join(str(p) for p in x.get('loc', ()))}: {x.get('msg')}" for x in errs)[:400]
            return False, REPAIR_TMPL.format(errors=compact)

    task = Task(description=user, expected_output=EXPECTED, agent=agent, guardrail=check, guardrail_max_retries=retries)
    await Crew(agents=[agent], tasks=[task], process=Process.sequential, memory=False, verbose=False).akickoff()
    if "errors" in state or "value" not in state:
        from .agents import AgentInvalidOutput

        raise AgentInvalidOutput(state.get("errors"))
    return state["value"]
