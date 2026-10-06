"""Insurer queries: intake, rounds, triage, grounded drafts, approvals, send, inbox (doc 07)."""

import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.services.queries import grounding_check
from tests.test_claims_api import acked, deliver, sql, status_of

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _reset_sim(capp: Any) -> None:
    capp.state.sim.fail_next, capp.state.sim.reject_code = 0, None
    for k in [k async for k in capp.state.redis.scan_iter("rl:hosp:ins-001:*")]:
        await capp.state.redis.delete(k)


def qbody(
    rnd: int = 1,
    cat: str = "billing_discrepancy",
    text: str = "Please explain the room rent charge of 1000.00 on the final bill.",
    docs: list[str] | None = None,
    qid: str | None = None,
) -> dict[str, Any]:
    return {
        "query": {
            "query_id": qid or str(uuid.uuid4()),
            "round": rnd,
            "category": cat,
            "text": text,
            "requested_doc_types": docs or [],
            "due_by": "2030-01-01T00:00:00Z",
            "status": "open",
            "raised_by": "adjudicator-1",
            "grounding": [],
        }
    }


async def raise_query(capp: Any, case: dict[str, Any], **kw: Any) -> str:
    body = qbody(**kw)
    r = await capp.state.sim.push(case["claim_ref"], "queries", body)
    assert r.status_code == 204, r.text
    return body["query"]["query_id"]


async def our_id(settings: Any, insurer_qid: str) -> str:
    return str(
        sql(settings, "SELECT id FROM insurer_query WHERE insurer_query_id=:i", i=insurer_qid)[0][0]
    )


async def test_grounding_table() -> None:
    ev = "Room rent 1,000.00 for 4 days. bill.pdf"
    good = grounding_check(
        "The room rent was 1000.00 as in the bill.",
        [{"source_id": "d1", "quote": "Room rent 1,000.00"}],
        ev,
    )
    assert good == []
    assert grounding_check("short", [], ev)[0]["rule"] == "G01"
    assert any(
        x["rule"] == "G02"
        for x in grounding_check("We guarantee this will be approved fully.", [], ev)
    )
    assert any(
        x["rule"] == "G03"
        for x in grounding_check(
            "The charge was 9999 rupees in total, as stated.",
            [{"source_id": "d", "quote": "Room rent"}],
            ev,
        )
    )
    assert any(
        x["rule"] == "G04"
        for x in grounding_check(
            "The room rent was as billed in the record.", [{"source_id": "d"}], ev
        )
    )
    assert any(
        x["rule"] == "G05"
        for x in grounding_check(
            "The room rent was as billed in the record.",
            [{"source_id": "d", "quote": "nonexistent text"}],
            ev,
        )
    )
    assert any(
        x["rule"] == "G06"
        for x in grounding_check(
            "The room rent was charged for 4 days as billed.", [], "Room 4 days"
        )
    )


async def test_three_rounds_end_to_end(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, sim = case["id"], capp.state.sim
    q1 = await raise_query(capp, case, rnd=1)
    assert await status_of(cclient, tok, cid) == "under_query"
    qid = await our_id(settings, q1)
    inbox = (await cclient.get("/v1/queries?status=open", headers=tok("desk1"))).json()
    assert any(i["id"] == qid for i in inbox["items"]) and inbox["counts"]["open"] >= 1
    assert all("patient_initials" in i and "full_name" not in i for i in inbox["items"])
    assert (await cclient.get("/v1/queries", headers=tok("hadmin"))).json()["items"] == []
    assert (await cclient.get(f"/v1/queries/{qid}", headers=tok("hadmin"))).status_code == 403
    # draft via the crew (grounded) and send with one approval
    assert (await cclient.post(f"/v1/queries/{qid}/draft", headers=tok("desk1"))).status_code == 202
    assert capp.state.crew.jobs[-1]["kind"] == "query-draft"
    draft = {
        "draft_text": "The room rent of 1000.00 is as shown on the final bill.",
        "citations": [{"source_id": "final_bill", "quote": "final_bill"}],
    }
    r = await cclient.post(
        f"/v1/internal/queries/{qid}/draft-result", json=draft, headers=tok("svc:crew")
    )
    assert r.status_code == 200 and r.json()["status"] == "draft", r.text
    assert (await cclient.post(f"/v1/queries/{qid}/send", headers=tok("officer1"))).json()[
        "code"
    ] == "not_approved"
    assert (
        await cclient.post(f"/v1/queries/{qid}/approve", headers=tok("desk1"), json={})
    ).status_code == 403
    assert (
        await cclient.post(f"/v1/queries/{qid}/approve", headers=tok("officer1"), json={})
    ).json()["approved"] is True
    r = await cclient.post(f"/v1/queries/{qid}/send", headers=tok("officer1"))
    assert r.status_code == 200, r.text
    assert await status_of(cclient, tok, cid) == "acknowledged"
    await deliver(capp, settings, cid)
    assert len(sim.responses) == 1 and str(sim.responses[0].query_id) == q1
    # round 2 then round 3 (always two approvers)
    q2 = await raise_query(
        capp, case, rnd=2, text="Please also clarify the pharmacy charges billed."
    )
    assert await status_of(cclient, tok, cid) == "under_query"
    q3 = await raise_query(
        capp,
        case,
        rnd=3,
        text="Final request: confirm the diagnosis supporting the stay.",
        cat="medical_clarification",
    )
    id3 = await our_id(settings, q3)
    assert (
        sql(settings, "SELECT status::text FROM insurer_query WHERE insurer_query_id=:i", i=q2)[0][
            0
        ]
        == "closed"
    )
    d3 = {
        "draft_text": "The diagnosis K80.2 supported the planned stay as recorded.",
        "citations": [],
    }
    r = await cclient.post(
        f"/v1/internal/queries/{id3}/draft-result", json=d3, headers=tok("svc:crew")
    )
    assert r.json()["status"] == "needs_attention"  # K80.2 not in the evidence -> G03 / G06 flagged
    r = await cclient.post(f"/v1/queries/{id3}/approve", headers=tok("officer1"), json={})
    assert r.status_code == 422 and r.json()["code"] == "override_note_required"
    note = {"override_note": "Diagnosis verified by hand against the discharge summary."}
    r = await cclient.post(f"/v1/queries/{id3}/approve", headers=tok("officer1"), json=note)
    assert r.json() == {"approved": False, "approvals_needed": 2, "version": 1}
    assert (
        await cclient.post(f"/v1/queries/{id3}/approve", headers=tok("officer1"), json=note)
    ).json()["code"] == "duplicate_approver"
    assert (
        await cclient.post(f"/v1/queries/{id3}/send", headers=tok("officer1"))
    ).status_code == 409
    assert (
        await cclient.post(f"/v1/queries/{id3}/approve", headers=tok("officer2"), json={})
    ).json()["approved"] is True
    assert (
        await cclient.post(f"/v1/queries/{id3}/send", headers=tok("officer2"))
    ).status_code == 200
    await deliver(capp, settings, cid)
    assert len(sim.responses) == 2
    assert await status_of(cclient, tok, cid) == "acknowledged"


async def test_round_four_rejected_and_audited(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    body = qbody(rnd=3)
    body["query"]["round"] = 4
    r = await capp.state.sim.push(case["claim_ref"], "queries", body)
    assert r.status_code == 422 and r.json()["code"] == "max_rounds_exceeded"
    assert "query.anomaly" in [
        x[0]
        for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=case["id"])
    ]


async def test_dedupe_reword_and_doc_requests(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    qid = str(uuid.uuid4())
    await raise_query(
        capp,
        case,
        qid=qid,
        cat="missing_document",
        docs=["discharge_summary"],
        text="Please send the discharge summary.",
    )
    ours = await our_id(settings, qid)
    rows = sql(
        settings,
        "SELECT status FROM doc_request WHERE case_id=:c AND rule_id='insurer_query' AND doc_type='discharge_summary'",
        c=case["id"],
    )
    assert rows and rows[0][0] == "open"
    await raise_query(
        capp,
        case,
        qid=qid,
        cat="missing_document",
        docs=["discharge_summary"],
        text="Please send the signed discharge summary.",
    )
    assert sql(settings, "SELECT revision FROM insurer_query WHERE id=:i", i=ours)[0][0] == 2
    assert (
        sql(settings, "SELECT count(*) FROM insurer_query WHERE insurer_query_id=:i", i=qid)[0][0]
        == 1
    )


async def test_overdue_notifies_once(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    q = await raise_query(capp, case)
    sql(
        settings,
        "UPDATE insurer_query SET due_by = now() - interval '1 hour' WHERE insurer_query_id=:i",
        i=q,
    )
    r = await cclient.post("/v1/internal/jobs/query-overdue", headers=tok("svc:n8n"))
    assert r.json()["notified"] >= 1
    assert (await cclient.post("/v1/internal/jobs/query-overdue", headers=tok("svc:n8n"))).json()[
        "notified"
    ] == 0
