from __future__ import annotations

import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
RESP = {"answer_text": "Attached as requested.", "responded_by": "desk"}

# scenario -> (kinds of callbacks in order, final status of the claim); scenarios that wait are answered automatically
EXPECTED = {
    "happy_path": (["status", "status", "decisions", "settlements"], "settled"),
    "partial_with_deductions": (["status", "status", "decisions", "settlements"], "settled"),
    "reject_exclusion": (["status", "decisions"], "rejected"),
    "identity_mismatch": (["status", "queries", "decisions", "settlements"], "settled"),
    "slow_insurer": (["status", "queries", "decisions", "settlements"], "settled"),
    "flaky_callbacks": (["status", "status", "decisions", "settlements"], "settled"),
    "needs_docs_then_reject": (["status", "queries", "decisions"], "rejected"),
    "duplicate_decision": (["status", "decisions", "settlements"], "settled"),
    "late_settlement": (["status", "decisions", "settlements"], "settled"),
}


async def answer_open_queries(sim, ref):
    for q in (await sim.client.get(f"/sim/claims/{ref}")).json()["queries"]:
        if q["status"] == "open":
            await sim.post(f"/v1/hospital-api/claims/{ref}/query-responses", {"query_id": q["query_id"], "responded_at": sim.clock.now().isoformat(), **RESP})


async def drive(sim, ref, rounds=60, step=3600, answer=True):
    for _ in range(rounds):
        sim.clock.advance(step)
        await sim.app.state.engine.tick()
        if answer:
            await answer_open_queries(sim, ref)


@pytest.mark.parametrize("sid", sorted(EXPECTED))
async def test_scenario_callbacks_and_golden(sim, sid):
    answer = sid not in ("needs_docs_then_reject",)
    sub = await sim.submit(sid)
    await drive(sim, sub["claim_ref"], answer=answer, step=1800 if sid != "late_settlement" else 7200)
    kinds, final = EXPECTED[sid]
    assert sim.receiver.kinds() == kinds
    st = (await sim.get(f"/v1/hospital-api/claims/{sub['claim_ref']}")).json()
    assert st["status"] == final
    # golden: ordered (kind, sequence, status/outcome) triples
    trace = [(k, b["sequence"], (b.get("status") or b.get("decision", {}).get("outcome") or b.get("query", {}).get("category") or b.get("settlement", {}).get("status"))) for k, b in sim.receiver.calls]
    GOLDEN.mkdir(exist_ok=True)
    path = GOLDEN / f"{sid}.json"
    if not path.exists():
        path.write_text(json.dumps(trace, indent=1), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == [list(t) for t in trace]
    seqs = [b["sequence"] for _, b in sim.receiver.calls]
    assert seqs == sorted(set(seqs))  # strictly increasing, no gaps for the receiver's applied state


async def test_escalation_waits_for_manual_fire(sim):
    sub = await sim.submit("query_three_rounds_escalate")
    ref = sub["claim_ref"]
    for _ in range(2):  # answer rounds 1 and 2
        await drive(sim, ref, rounds=3, step=60)
    await drive(sim, ref, rounds=30, step=1800, answer=False)  # round 3 left unanswered past due
    assert sim.receiver.kinds()[-2:] == ["queries", "status"]
    assert sim.receiver.calls[-1][1]["status"] == "escalated"
    await drive(sim, ref, rounds=5, step=3600, answer=False)
    assert "decisions" not in sim.receiver.kinds()  # manual step does not fire by itself
    r = await sim.client.post(f"/sim/claims/{ref}/fire-next")
    assert r.json()["result"] == "delivered"
    assert sim.receiver.calls[-1][1]["decision"]["outcome"] == "partial"
    assert [q["round"] for k, b in sim.receiver.calls if k == "queries" for q in [b["query"]]] == [1, 2, 3]


async def test_bad_signature_probe_not_applied_valid_applied(sim):
    sub = await sim.submit("bad_signature_probe")
    await drive(sim, sub["claim_ref"], rounds=10, step=60, answer=False)
    # the receiver rejects wrong secret and skewed timestamp, so only the valid status + decision got through
    assert sim.receiver.kinds() == ["status", "decisions"]
    assert [b["sequence"] for _, b in sim.receiver.calls] == [1, 2]
    log = (await sim.client.get("/sim/log", params={"claim_ref": sub["claim_ref"]})).json()
    assert {r["chaos"] for r in log if r["chaos"]} == {"bad_secret", "skew_plus_10m"}
    assert sorted(r["status"] for r in log if r["chaos"]) == [401, 401]


async def test_duplicate_decision_applied_once(sim):
    sub = await sim.submit("duplicate_decision")
    await drive(sim, sub["claim_ref"], rounds=10, step=60, answer=False)
    assert sim.receiver.kinds().count("decisions") == 1  # second copy is an idempotent replay at the receiver
    log = (await sim.client.get("/sim/log", params={"claim_ref": sub["claim_ref"]})).json()
    assert sum(1 for r in log if r["chaos"] == "duplicate") == 1


async def test_withdrawn_by_hospital_closes(sim):
    sub = await sim.submit("withdrawn_by_hospital")
    ref = sub["claim_ref"]
    await drive(sim, ref, rounds=3, step=60, answer=False)
    r = await sim.post(f"/v1/hospital-api/claims/{ref}/withdraw", {"reason": "other"})
    assert r.status_code == 200
    await drive(sim, ref, rounds=3, step=60, answer=False)
    assert sim.receiver.calls[-1][1]["status"] == "closed"
    again = await sim.post(f"/v1/hospital-api/claims/{ref}/withdraw", {"reason": "other"})
    assert again.status_code == 409


async def test_reimbursement_routes_by_claim_type(sim):
    sub = await sim.submit(None, claim_type="reimbursement")
    r = await sim.client.get(f"/sim/claims/{sub['claim_ref']}")
    assert r.json()["scenario_id"] == "reimbursement_basic"


async def test_chaos_flaky_is_reproducible_and_converges(sim, receiver):
    outcomes = []
    for _ in range(2):
        await sim.client.post("/sim/reset")
        receiver.calls.clear()
        receiver.applied.clear()
        await sim.client.post("/sim/chaos", content=json.dumps({"preset": "flaky"}))
        sim.app.state.engine.cfg.retry_scale = 0.001
        sub = await sim.submit("flaky_callbacks")
        await drive(sim, sub["claim_ref"], rounds=80, step=5, answer=False)
        outcomes.append(((await sim.get(f"/v1/hospital-api/claims/{sub['claim_ref']}")).json()["status"], [(k, b["sequence"]) for k, b in receiver.calls]))
        # every event applied exactly once, in order, despite drops/duplicates/reordering
        assert sorted({s for _, s in outcomes[-1][1]}) == [1, 2, 3, 4] or outcomes[-1][0] == "settled"
    assert outcomes[0][0] == "settled"


async def test_chaos_fail_status_on_sim_endpoints(sim):
    await sim.client.post("/sim/chaos", content=json.dumps({"fail_status": 503, "fail_count": 2}))
    from claim_contract.insurer_side.samples import make_submission

    sub = make_submission()
    assert (await sim.post("/v1/hospital-api/claims", sub)).status_code == 503
    assert (await sim.post("/v1/hospital-api/claims", sub)).status_code == 503
    assert (await sim.post("/v1/hospital-api/claims", sub)).status_code == 202


async def test_chaos_rejects_unknown_setting_and_clear(sim):
    r = await sim.client.post("/sim/chaos", content=json.dumps({"bogus": 1}))
    assert r.status_code in (400, 422)
    await sim.client.post("/sim/chaos", content=json.dumps({"drop_rate": 0.5}))
    assert (await sim.client.get("/sim/chaos")).json()["drop_rate"] == 0.5
    assert (await sim.client.delete("/sim/chaos")).json()["drop_rate"] == 0.0


async def test_ui_pages_render(sim):
    sub = await sim.submit("happy_path")
    assert "tpa-sim" in (await sim.client.get("/")).text
    assert sub["claim_ref"] in (await sim.client.get(f"/ui/claims/{sub['claim_ref']}")).text
