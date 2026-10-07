"""The real crew (fake model) talking to the real API: claim build, triage and grounded draft end to end."""

import asyncio
from typing import Any

import httpx
import pytest
import pytest_asyncio
from crew.llm import FakeLLM
from crew.main import create_app as create_crew
from crew.settings import Settings as CrewSettings
from tests.claims_helpers import ready_case
from tests.test_claims_api import (
    _reset_sim,  # noqa: F401  (autouse reset of the shared insurer sim)
    status_of,
)
from tests.test_queries_api import our_id, raise_query

pytestmark = pytest.mark.integration


class ViaClient:
    """Crew's API client wired straight to the test app with the crew service token."""

    def __init__(self, c: httpx.AsyncClient, headers: dict[str, str]) -> None:
        self.c, self.h = c, headers

    async def get(self, path: str) -> dict[str, Any]:
        r = await self.c.get(path, headers=self.h)
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self.c.post(path, json=body, headers=self.h)
        r.raise_for_status()
        return r.json() if r.content else {}  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def crew_app(cclient: httpx.AsyncClient, tok: Any):  # type: ignore[no-untyped-def]
    holder: dict[str, Any] = {}

    def make(llm: FakeLLM):  # type: ignore[no-untyped-def]
        app = create_crew(CrewSettings(), llm=llm, api=ViaClient(cclient, tok("svc:crew")))
        holder["app"] = app
        return app

    return make


async def run_job(app: Any, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    job = app.state.runner.submit(kind, body)
    await asyncio.wait_for(asyncio.gather(*app.state.runner.tasks), 20)
    return job.envelope()


async def test_claim_build_by_the_real_crew_passes_the_api_validators(
    cclient, tok, capp, crew_app, settings
):  # type: ignore[no-untyped-def]
    case = await ready_case(cclient, tok, pharmacy_total="1000.00")
    r = await cclient.post(f"/v1/cases/{case['id']}/claim/build", headers=tok("desk1"))
    assert r.status_code == 202
    sent = capp.state.crew.jobs[-1]
    env = await run_job(crew_app(FakeLLM(default={"items": []})), "claim-build", sent)
    assert env["state"] == "succeeded", env["error"]
    assert await status_of(cclient, tok, case["id"]) == "ready_for_review"
    d = (await cclient.get(f"/v1/cases/{case['id']}/claim", headers=tok("officer1"))).json()
    assert d["has_errors"] is False
    assert d["payload"]["totals"]["gross"] == d["payload"]["totals"]["claimed"]
    assert d["payload"]["patient"]["full_name"] == "Ravi Kumar"  # from the case, never from a model


async def test_triage_then_grounded_draft_is_accepted_without_flags(
    cclient, tok, capp, crew_app, settings
):  # type: ignore[no-untyped-def]
    from tests.test_claims_api import acked

    case = await acked(cclient, tok, capp, settings)
    q = await raise_query(capp, case, text="Please explain the room rent charge on the final bill.")
    qid = await our_id(settings, q)
    llm = FakeLLM(
        {
            "<query>": {
                "action": "clarify",
                "needs_docs": False,
                "escalation_risk": False,
                "note": "billing question",
            },
            "Sources:": {
                "draft_text": "The room rent charge is explained on the final bill in the record.",
                "citations": [{"source_id": "S1", "quote": "explain the room rent charge"}],
                "missing": [],
            },
        }
    )
    app = crew_app(llm)
    assert (await run_job(app, "query-triage", {"query_id": qid}))["state"] == "succeeded"
    row = (await cclient.get(f"/v1/queries/{qid}", headers=tok("officer1"))).json()
    assert row["triage_source"] == "crew" and row["triage"]["action"] == "clarify"
    env = await run_job(app, "query-draft", {"query_id": qid})
    assert env["state"] == "succeeded", env["error"]
    assert env["result"]["unsupported"] == []
    resp = (await cclient.get(f"/v1/queries/{qid}", headers=tok("officer1"))).json()["responses"][0]
    assert (
        resp["status"] == "draft" and resp["source"] == "agent" and resp["unsupported_claims"] == []
    )
    # no identity left for the model: the prompts never saw the patient's name
    assert all("Ravi Kumar" not in p for _, p in llm.calls)
