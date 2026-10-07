"""CrewAI Flows over the seven insurer agents. Each step runs one agent through the service's own `execute` (auth already
done; context validation, PII scan, idempotency cache, concurrency gate, tracing and output checks all apply), so a flow
step and a per-agent HTTP call give the same answer. Gates and money stay with insurer-api and calc-engine.

    VerificationFlow: identity -> authenticity -> coverage -> calc_mapper -> supervisor -> route (proceed | review)
    QueryFlow:        route (draft | triage) -> query_drafter | triage
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from claim_contract.errors import ProblemError
from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel, Field, PrivateAttr

from .agents import SPECS, _has_blocker
from .schemas import AgentRequest

Execute = Callable[[str, AgentRequest], Awaitable[dict[str, Any]]]
VERIFICATION_STEPS = ("identity", "authenticity", "coverage", "calc_mapper")


class FlowState(BaseModel):
    id: str = ""
    request_id: str = ""
    case_id: str = ""
    contexts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    kind: str = "draft"  # QueryFlow only: draft | triage
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: list[str] = Field(default_factory=list)
    outcome: str = ""


class _AgentFlow(Flow[FlowState]):
    _execute: Any = PrivateAttr(default=None)

    @classmethod
    def create(cls, execute: Execute, *, quiet: bool = True) -> Any:
        f = cls(suppress_flow_events=quiet)
        f._execute = execute
        return f

    async def run_agent(self, name: str, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Run one agent if it has a context; a failed step is recorded as ``{"failure": code}`` (a blocker for the supervisor)."""
        s = self.state
        ctx = context if context is not None else s.contexts.get(name)
        if ctx is None:
            return None
        s.steps.append(name)
        rid = uuid.uuid5(uuid.UUID(s.request_id), name)  # stable per step: a retried flow hits the idempotency cache
        req = AgentRequest(request_id=rid, case_id=s.case_id, context=ctx, options=s.options)
        try:
            out = await self._execute(name, req)
        except ProblemError as e:
            out = {"failure": e.code, "status": e.status, "detail": (e.detail or "")[:300]}
        s.outputs[name] = out
        return out


class VerificationFlow(_AgentFlow):
    """Verification crew: identity, authenticity, coverage and calc mapper, then the supervisor's summary."""

    @start()
    async def check_identity(self) -> None:
        await self.run_agent("identity")

    @listen(check_identity)
    async def check_authenticity(self) -> None:
        await self.run_agent("authenticity")

    @listen(check_authenticity)
    async def check_coverage(self) -> None:
        await self.run_agent("coverage")

    @listen(check_coverage)
    async def map_bill_lines(self) -> None:
        await self.run_agent("calc_mapper")

    @listen(map_bill_lines)
    async def summarise(self) -> None:
        done = {k: v for k, v in self.state.outputs.items() if k in VERIFICATION_STEPS}
        ctx = dict(self.state.contexts.get("supervisor") or {})
        ctx["step_outputs"] = {**done, **(ctx.get("step_outputs") or {})}
        if ctx["step_outputs"]:
            await self.run_agent("supervisor", ctx)

    @router(summarise, emit=["proceed", "review"])
    async def route(self) -> str:
        sup = self.state.outputs.get("supervisor") or {}
        if sup.get("recommended_next_action") == "proceed" and not _has_blocker(self.state.outputs):
            return "proceed"
        return "review"

    @listen("proceed")
    async def ready_for_gate(self) -> dict[str, Any]:
        self.state.outcome = "proceed"
        return self._result()

    @listen("review")
    async def needs_human_review(self) -> dict[str, Any]:
        self.state.outcome = "review"
        return self._result()

    def _result(self) -> dict[str, Any]:
        sup = self.state.outputs.get("supervisor") or {}
        return {"outcome": self.state.outcome, "recommended_next_action": sup.get("recommended_next_action"),
                "steps": list(self.state.steps), "outputs": self.state.outputs}  # fmt: skip


class QueryFlow(_AgentFlow):
    """Query crew: the query drafter writes the query; the triage agent reads the hospital's reply."""

    @start()
    async def choose(self) -> None:
        self.state.steps.append("choose")

    @router(choose, emit=["draft", "triage"])
    async def route(self) -> str:
        if self.state.kind == "triage":
            return "triage"
        return "draft"

    @listen("draft")
    async def draft_query(self) -> dict[str, Any]:
        out = await self.run_agent("query_drafter")
        return {"steps": list(self.state.steps), "output": out}

    @listen("triage")
    async def triage_reply(self) -> dict[str, Any]:
        out = await self.run_agent("triage")
        return {"steps": list(self.state.steps), "output": out}


FLOWS = {"verification": VerificationFlow, "query": QueryFlow}
assert set(VERIFICATION_STEPS) | {"supervisor", "query_drafter", "triage"} == set(SPECS)  # noqa: S101 - every agent has a flow
