"""CrewAI agents, tasks and crews (doc 09 roles). `run_task` builds one agent from `config/agents.yaml`, gives it one task
whose description is the rendered versioned prompt, and runs it as a sequential crew. Guardrails are the existing
deterministic checks; a failed guardrail sends its complaint back to the agent (CrewAI retries the task with it)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from crewai import Agent, Crew, Process, Task, TaskOutput

from crew.crewai_bridge import BridgeLLM
from crew.llm import LLM, SchemaInvalid

AGENTS: dict[str, dict[str, str]] = yaml.safe_load(
    (Path(__file__).parent / "config" / "agents.yaml").read_text()
)

Guardrail = Callable[[dict[str, Any], int], str | None]
"""(parsed JSON answer, attempt number starting at 1) -> complaint to send back, or None to accept."""


@dataclass
class TaskResult:
    data: dict[str, Any]
    model_info: dict[str, Any]
    attempts: int


def build_agent(name: str, llm: BridgeLLM) -> Agent:
    spec = AGENTS[name]
    return Agent(
        role=spec["role"],
        goal=spec["goal"],
        backstory=spec["backstory"].strip(),
        llm=llm,
        allow_delegation=False,
        max_iter=2,
        max_retry_limit=0,  # model errors (PII, unavailable) must reach the job runner with their own type
        verbose=False,
    )


def _parse(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise SchemaInvalid(f"agent answer is not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SchemaInvalid("agent answer is not a JSON object")
    return obj


async def run_task(name: str, prompt: str, llm: LLM, model: str, *, guardrail: Guardrail | None = None,
                   retries: int = 1) -> TaskResult:  # fmt: skip
    bridge = BridgeLLM.over(llm, model)
    agent = build_agent(name, bridge)
    attempts = 0

    def check(out: TaskOutput):  # noqa: ANN202 - CrewAI rejects a string return annotation here
        nonlocal attempts
        attempts += 1
        complaint = guardrail(_parse(out.raw), attempts) if guardrail else None
        return (True, out) if complaint is None else (False, complaint)

    task = Task(
        description=prompt,
        expected_output="One JSON object exactly as the instructions describe, with no other text.",
        agent=agent,
        guardrail=check if guardrail else None,
        guardrail_max_retries=retries,
    )
    crew = Crew(
        agents=[agent], tasks=[task], process=Process.sequential, memory=False, verbose=False
    )
    out = await crew.akickoff()
    return TaskResult(_parse(out.raw), bridge.last_info, max(attempts, bridge.calls))
