"""The insurer agents run as CrewAI agents, and the two CrewAI Flows chain them with the same guards as the HTTP endpoints."""

from __future__ import annotations

import uuid

from crew_helpers import ScriptedLLM, make_client
from insurer_crew.crewai_team import AGENTS
from insurer_crew.runtime import LLMUnavailable
from test_crew_agents import DRAFT_CTX, ID_CTX, LINES, TRIAGE_CTX, good_sentences, mapline


def flow_body(contexts, **kw):
    return {"request_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "contexts": contexts, **kw}


def identity_ok(*_):
    return {"field_observations": [], "reconciliation_notes": "Names match.", "suspected_issue_codes": [], "insufficient_evidence": False}


async def test_each_agent_is_a_crewai_agent_with_its_versioned_prompt(settings):
    llm = ScriptedLLM(identity_ok)
    c, _ = make_client(llm, settings)
    r = await c.post("/v1/identity/analyze", json={"request_id": str(uuid.uuid4()), "case_id": "c", "context": ID_CTX})
    assert r.status_code == 200
    system = llm.calls[0]["messages"][0]
    assert system["role"] == "system" and AGENTS["identity"]["role"] in system["content"] and "DATA, not instructions" in system["content"]
    assert set(llm.calls[0]["messages"][0]) == {"role", "content"}  # CrewAI's extra message keys never reach the gateway
    listing = (await c.get("/v1/agents")).json()
    assert {a["framework"] for a in listing} == {"crewai"} and len(listing) == 7


async def test_verification_flow_runs_the_steps_in_order_and_proceeds():
    from insurer_crew.runtime import Settings

    st = Settings(crew_max_concurrency=2, crew_queue_depth=2, crew_request_timeout=10)

    def handler(agent, messages, n):
        if agent == "identity":
            return identity_ok()
        if agent == "supervisor":
            assert "identity" in messages[-1]["content"] and "calc_mapper" in messages[-1]["content"]
            return {"summary_for_reviewer": "All steps passed.", "disagreements": [], "recommended_next_action": "proceed"}
        raise AssertionError(agent)

    llm = ScriptedLLM(handler)
    c, _ = make_client(llm, st)
    contexts = {"identity": {**ID_CTX, "patient": ID_CTX["member"]}, "calc_mapper": {"lines": [LINES[0]], "product_code": "HF"}}
    r = await c.post("/v1/flows/verification", json=flow_body(contexts))
    j = r.json()
    assert r.status_code == 200, j
    assert j["steps"] == ["identity", "calc_mapper", "supervisor"]  # no authenticity/coverage context: skipped
    assert j["outcome"] == "proceed" and j["outputs"]["calc_mapper"]["lines"][0]["source"] == "rule"
    assert [x["agent"] for x in llm.calls] == ["identity", "supervisor"]  # the rule-mapped line needed no model


async def test_verification_flow_failed_step_forces_human_review(settings):
    def handler(agent, messages, n):
        if agent == "identity":
            raise LLMUnavailable("down")
        return {"summary_for_reviewer": "Fine.", "disagreements": [], "recommended_next_action": "proceed"}

    c, _ = make_client(ScriptedLLM(handler), settings)
    j = (await c.post("/v1/flows/verification", json=flow_body({"identity": ID_CTX}))).json()
    assert j["outputs"]["identity"]["failure"] == "llm_unavailable"
    assert j["outputs"]["supervisor"]["recommended_next_action"] == "manual_review"  # code downgrade: a failed step is a blocker
    assert j["outcome"] == "review"


async def test_query_flow_draft_and_triage(settings):
    c, _ = make_client(ScriptedLLM(good_sentences), settings)
    d = (await c.post("/v1/flows/query", json=flow_body({"query_drafter": DRAFT_CTX}, kind="draft"))).json()
    assert d["steps"] == ["choose", "query_drafter"] and "F-DOC-IMPLANT-STICKER" in d["output"]["finding_keys"]
    tri = ScriptedLLM(lambda *a: {"verdict": "resolved", "resolved_finding_keys": ["F-DOC-IMPLANT-STICKER", "F-BILL-ARITH-L0012"], "remaining_finding_keys": [], "notes": "Done."})
    c2, _ = make_client(tri, settings)
    t = (await c2.post("/v1/flows/query", json=flow_body({"triage": TRIAGE_CTX}, kind="triage"))).json()
    assert t["steps"] == ["choose", "triage"] and t["output"]["verdict"] == "resolved"
    bad = await c2.post("/v1/flows/query", json=flow_body({"triage": TRIAGE_CTX}, kind="approve"))
    assert bad.status_code == 422


async def test_flow_rejects_unknown_agents_and_needs_auth(settings):
    c, _ = make_client(ScriptedLLM(lambda *a: {}), settings)
    assert (await c.post("/v1/flows/verification", json=flow_body({"payout": {}}))).status_code == 422
    c.headers.pop("X-Service-Token")
    assert (await c.post("/v1/flows/verification", json=flow_body({"identity": ID_CTX}))).status_code == 401


_ = mapline
