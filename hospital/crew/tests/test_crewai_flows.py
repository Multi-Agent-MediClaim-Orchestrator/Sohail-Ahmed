"""The jobs really run as CrewAI flows and agents: role prompts reach the model, flows route, guards still gate."""

import pytest
from crew.flows import ClaimFlow, FlowDeps, QueryFlow
from crew.guards.pii import PiiDetected
from crew.jobs import Runner
from crew.llm import FakeLLM, RulesLLM
from crew.settings import Settings
from crew.team import AGENTS
from test_crew import EVID, FakeApi, ctx

ST = Settings()


async def test_claim_flow_build_runs_the_category_mapper_agent():
    api = FakeApi({"/v1/internal/cases/c1/build-context": ctx()})
    llm = FakeLLM({"<lines>": {"items": [{"index": 1, "category": "consumable"}]}})
    flow = ClaimFlow.create(FlowDeps(llm, api, ST))
    out = await flow.kickoff_async(inputs={"case_id": "c1", "job_id": "j1"})
    assert out == {"posted": True, "repair_round": 0}
    assert flow.state.steps == ["load_context", "build_claim", "post_draft"]
    model, prompt = llm.calls[0]
    assert (
        model == ST.model_local and AGENTS["category_mapper"]["role"] in prompt
    )  # CrewAI's agent framing
    path, body = api.posts[0]
    assert path == "/v1/internal/cases/c1/claim/draft" and body["job_id"] == "j1"


async def test_claim_flow_routes_to_repair():
    first = FakeApi({"/v1/internal/cases/c1/build-context": ctx()})
    await ClaimFlow.create(FlowDeps(RulesLLM(), first, ST)).kickoff_async(
        inputs={"case_id": "c1", "job_id": "j0"}
    )
    previous = first.posts[0][1]["payload"]
    api = FakeApi({"/v1/internal/cases/c1/build-context": ctx() | {"previous_draft": previous}})
    flow = ClaimFlow.create(FlowDeps(RulesLLM(), api, ST))
    out = await flow.kickoff_async(
        inputs={"case_id": "c1", "job_id": "j2", "repair": {"round": 2, "errors": []}}
    )
    assert out["repair_round"] == 2 and flow.state.steps == [
        "load_context",
        "repair_claim",
        "post_draft",
    ]


async def test_query_flow_draft_retries_through_the_guardrail():
    bad = {
        "draft_text": "The room rent was 99999 as recorded in the file.",
        "citations": [],
        "missing": [],
    }
    good = {"draft_text": "The room rent of 10000.00 is as billed in the record.",
            "citations": [{"source_id": "S2", "quote": '"description": "Room rent"'}], "missing": []}  # fmt: skip
    llm = FakeLLM({"Sources:": [bad, good]})
    api = FakeApi({"/v1/internal/queries/q/context": EVID})
    flow = QueryFlow.create(FlowDeps(llm, api, ST))
    out = await flow.kickoff_async(inputs={"query_id": "q", "kind": "draft"})
    assert out["unsupported"] == [] and out["supervisor"]["pass"] is True
    assert len(llm.calls) == 2 and "number not found in the record: 99999" in llm.calls[1][1]
    assert (
        flow.state.steps == ["load_query", "draft_reply"]
        and api.posts[0][0] == "/v1/internal/queries/q/draft-result"
    )


async def test_pii_in_a_prompt_fails_the_job_with_its_own_code():
    q = {"id": "q9", "category": "other", "round": 1, "text": "member Aadhaar 1234 5678 9012"}
    api = FakeApi({"/v1/internal/queries/q9": q})
    llm = FakeLLM(default={"action": "clarify"})
    with pytest.raises(PiiDetected):
        await QueryFlow.create(FlowDeps(llm, api, ST)).kickoff_async(
            inputs={"query_id": "q9", "kind": "triage"}
        )
    assert llm.calls == [] and api.posts == []

    async def handler(job):
        return await QueryFlow.create(FlowDeps(llm, api, ST)).kickoff_async(
            inputs={"query_id": "q9", "kind": "triage"}
        )

    r = Runner({"query-triage": handler})
    job = r.submit("query-triage", {"query_id": "q9"})
    for t in list(r.tasks):
        await t
    assert job.state == "failed" and job.error["code"] == "pii_detected"
