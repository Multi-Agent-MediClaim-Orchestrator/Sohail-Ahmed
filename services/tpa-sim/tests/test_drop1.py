from __future__ import annotations

import uuid

import pytest
from claim_contract.insurer_side.samples import make_submission
from tpa_sim import dsl


async def test_happy_path(sim):
    sub = await sim.submit("happy_path")
    await sim.run()
    assert sim.receiver.kinds() == ["status", "status", "decisions", "settlements"]
    assert [b["sequence"] for _, b in sim.receiver.calls] == [1, 2, 3, 4]
    dec = sim.receiver.calls[2][1]["decision"]
    assert dec["outcome"] == "approve" and dec["approved_amount"]["amount"] == sub["totals"]["claimed"]["amount"]
    r = await sim.get(f"/v1/hospital-api/claims/{sub['claim_ref']}")
    assert r.json()["status"] == "settled"


async def test_submission_ack_and_idempotent_replay(sim):
    sub = make_submission()
    idem = str(uuid.uuid4())
    r1 = await sim.post("/v1/hospital-api/claims", sub, idem)
    r2 = await sim.post("/v1/hospital-api/claims", sub, idem)
    assert r1.status_code == 202 and r1.json()["insurer_claim_no"].startswith("IC-")
    assert r2.json()["insurer_claim_no"] == r1.json()["insurer_claim_no"]
    r3 = await sim.post("/v1/hospital-api/claims", sub)  # new idempotency key, same content
    assert r3.status_code == 200
    sub["hospital_notes"] = "changed"
    r4 = await sim.post("/v1/hospital-api/claims", sub)
    assert r4.status_code == 409


async def test_unsigned_request_rejected(sim):
    r = await sim.client.post("/v1/hospital-api/claims", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 401


async def test_query_twice_waits_for_responses(sim):
    sub = await sim.submit("query_twice_then_approve")
    ref = sub["claim_ref"]
    await sim.run()
    assert sim.receiver.kinds() == ["status", "queries"]  # held at wait_response
    q1 = sim.receiver.calls[-1][1]["query"]
    assert q1["round"] == 1 and q1["status"] == "open"
    answer = {"answer_text": "Attached the summary.", "responded_by": "desk", "responded_at": sim.clock.now().isoformat()}
    r = await sim.post(f"/v1/hospital-api/claims/{ref}/query-responses", {"query_id": q1["query_id"], **answer})
    assert r.status_code == 202, r.text
    await sim.run()
    assert sim.receiver.kinds()[-1] == "queries"
    q2 = sim.receiver.calls[-1][1]["query"]
    assert q2["round"] == 2 and q2["query_id"] != q1["query_id"]
    await sim.post(f"/v1/hospital-api/claims/{ref}/query-responses", {"query_id": q2["query_id"], **answer})
    await sim.run()
    assert sim.receiver.kinds()[-3:] == ["status", "decisions", "settlements"]


async def test_callback_retry_keeps_sequence_and_body(sim):
    await sim.submit("happy_path")
    sim.receiver.fail_next = [503, 503]
    await sim.run(step=2, max_iter=80)
    assert sim.receiver.kinds().count("decisions") == 1
    assert [b["sequence"] for _, b in sim.receiver.calls] == [1, 2, 3, 4]


async def test_withdraw_stops_future_callbacks(sim):
    sub = await sim.submit("happy_path")
    ref = sub["claim_ref"]
    sim.clock.advance(3)
    await sim.app.state.engine.tick()
    r = await sim.post(f"/v1/hospital-api/claims/{ref}/withdraw", {"reason": "patient_requested"})
    assert r.status_code == 200, r.text
    n = len(sim.receiver.calls)
    await sim.run()
    new = sim.receiver.calls[n:]
    assert [(k, b["status"]) for k, b in new] == [("status", "closed")]  # only the closing notice


async def test_pause_resume(sim):
    sub = await sim.submit("happy_path")
    ref = sub["claim_ref"]
    pr = await sim.client.post(f"/sim/claims/{ref}/pause")
    assert pr.status_code == 200, pr.text
    await sim.run()
    assert sim.receiver.calls == []
    await sim.client.post(f"/sim/claims/{ref}/resume")
    await sim.run()
    assert len(sim.receiver.calls) == 4


async def test_custom_scenario_upload_and_validation(sim):
    bad = await sim.client.post("/sim/scenarios", content="id: x\nsteps: []\nbogus: 1")
    assert bad.status_code in (400, 422)
    ok = await sim.client.post("/sim/scenarios", content="id: quick_reject\nsteps:\n  - {id: d, kind: decision, outcome: reject, after: 1s}\n")
    assert ok.status_code == 200
    await sim.submit("quick_reject")
    await sim.run()
    d = sim.receiver.calls[0][1]["decision"]
    assert d["outcome"] == "reject" and d["approved_amount"]["amount"] == "0.00"


def test_dsl_rules():
    assert dsl.parse_duration("2m") == 120 and dsl.parse_duration("500ms") == 0.5
    with pytest.raises(ValueError):
        dsl.parse_scenario("id: a\nsteps:\n  - {id: w, kind: wait_response}\n")
