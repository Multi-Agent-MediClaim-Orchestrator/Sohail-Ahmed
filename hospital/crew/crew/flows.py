"""CrewAI Flows that run the hospital crew's jobs (doc 09 §4). A Flow owns the job's state and the order of steps;
the agents inside (crew/team.py) do the model work; deterministic code builds totals and gates the output. The
hospital API stays the only writer: every flow ends by posting its result to an internal endpoint.

    ClaimFlow:  load_context -> route (build | repair) -> build_claim | repair_claim -> post_draft
    QueryFlow:  load_query  -> route (triage | draft)  -> triage_query | draft_reply  -> (posted inside the step)
"""

from __future__ import annotations

import asyncio
from typing import Any

from crewai.flow.flow import Flow, listen, or_, router, start
from pydantic import BaseModel, Field, PrivateAttr

from crew.agents import builder, responder
from crew.api_client import ApiClient
from crew.llm import LLM
from crew.settings import Settings


def flat(info: dict[str, Any]) -> dict[str, str | int | float | None]:
    return {k: v for k, v in info.items() if isinstance(v, (str, int, float)) or v is None}


class FlowDeps:
    """What a flow needs from the service; set after construction (Flow is a pydantic model)."""

    def __init__(
        self, llm: LLM, api: ApiClient, st: Settings, cancel: asyncio.Event | None = None
    ) -> None:
        self.llm, self.api, self.st = llm, api, st
        self.cancel = cancel or asyncio.Event()


class ClaimState(BaseModel):
    id: str = ""
    case_id: str = ""
    job_id: str = ""
    callback: str | None = None
    repair: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    draft: dict[str, Any] = Field(default_factory=dict)
    repair_round: int = 0
    steps: list[str] = Field(default_factory=list)


class ClaimFlow(Flow[ClaimState]):
    """claim-build and claim-repair."""

    _deps: Any = PrivateAttr(default=None)

    @classmethod
    def create(cls, deps: FlowDeps, *, quiet: bool = True) -> ClaimFlow:
        f = cls(suppress_flow_events=quiet)
        f._deps = deps
        return f

    @start()
    async def load_context(self) -> None:
        self.state.steps.append("load_context")
        self.state.context = await self._deps.api.get(
            f"/v1/internal/cases/{self.state.case_id}/build-context"
        )

    @router(load_context, emit=["build", "repair"])
    async def route(self) -> str:
        if self.state.repair:
            return "repair"
        return "build"

    @listen("build")
    async def build_claim(self) -> None:
        self.state.steps.append("build_claim")
        self.state.draft = await builder.build(self.state.context, self._deps.llm, self._deps.st)

    @listen("repair")
    async def repair_claim(self) -> None:
        self.state.steps.append("repair_claim")
        prev = self.state.context.get("previous_draft")
        if prev is None:
            raise ValueError("no previous draft to repair")
        rep = self.state.repair or {}
        self.state.draft = await builder.repair(
            prev, rep.get("errors", []), self._deps.llm, self._deps.st
        )
        self.state.repair_round = int(rep.get("round", 1))

    @listen(or_(build_claim, repair_claim))
    async def post_draft(self) -> dict[str, Any]:
        if self._deps.cancel.is_set():
            return {"cancelled": True}
        self.state.steps.append("post_draft")
        out, s = self.state.draft, self.state
        body = {"job_id": s.job_id, "payload": out["payload"], "provenance": out["provenance"],
                "model_info": flat(out["model_info"]), "repair_round": s.repair_round}  # fmt: skip
        await self._deps.api.post(s.callback or f"/v1/internal/cases/{s.case_id}/claim/draft", body)
        return {"posted": True, "repair_round": s.repair_round}


class QueryState(BaseModel):
    id: str = ""
    query_id: str = ""
    kind: str = "triage"  # triage | draft
    query: dict[str, Any] = Field(default_factory=dict)
    steps: list[str] = Field(default_factory=list)


class QueryFlow(Flow[QueryState]):
    """query-triage and query-draft."""

    _deps: Any = PrivateAttr(default=None)

    @classmethod
    def create(cls, deps: FlowDeps, *, quiet: bool = True) -> QueryFlow:
        f = cls(suppress_flow_events=quiet)
        f._deps = deps
        return f

    @start()
    async def load_query(self) -> None:
        self.state.steps.append("load_query")
        path = f"/v1/internal/queries/{self.state.query_id}"
        self.state.query = await self._deps.api.get(
            path if self.state.kind == "triage" else path + "/context"
        )

    @router(load_query, emit=["triage", "draft"])
    async def route(self) -> str:
        if self.state.kind == "draft":
            return "draft"
        return "triage"

    @listen("triage")
    async def triage_query(self) -> dict[str, Any]:
        self.state.steps.append("triage_query")
        q = self.state.query
        out = await responder.triage(q, self._deps.llm, self._deps.st)
        mi = out.pop("_model_info")
        await self._deps.api.post(f"/v1/internal/queries/{q['id']}/triage-result", out)
        return out | {"model_info": mi}

    @listen("draft")
    async def draft_reply(self) -> dict[str, Any]:
        self.state.steps.append("draft_reply")
        ctx, qid = self.state.query, self.state.query_id
        out = await responder.draft(ctx, self._deps.llm, self._deps.st)
        body = {"draft_text": out["draft_text"], "citations": out["citations"], "attached_doc_ids": ctx.get("attach_doc_ids", []),
                "model_info": flat(out["model_info"] | {"supervisor_pass": int(out["supervisor"]["pass"])})}  # fmt: skip
        await self._deps.api.post(f"/v1/internal/queries/{qid}/draft-result", body)
        return {"supervisor": out["supervisor"], "unsupported": out["unsupported"]}
