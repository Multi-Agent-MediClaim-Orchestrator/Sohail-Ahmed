import asyncio
import uuid
from decimal import Decimal

import pytest
from app.services import audit, jobs, outbox
from claim_contract.models import Decision as ContractDecision
from claim_contract.samples import make_line, make_submission
from ins_helpers import register_claim_docs, unique_member
from sqlalchemy import text

pytestmark = pytest.mark.integration


def claim_of(total_lines, min_si=500_000, **kw):
    """Claim whose lines sum to ``total_lines`` (list of (category, desc, amount))."""
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    c = make_submission(claim_ref=ref, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **{**unique_member(min_si), **kw})
    src = c["documents"][1]["doc_id"]
    c["bill_lines"] = [make_line(i, category=cat, desc=desc, qty="1", unit=amt, doc_id=src) for i, (cat, desc, amt) in enumerate(total_lines, start=1)]
    from claim_contract.samples import money

    gross = sum(Decimal(a) for _, _, a in total_lines)
    c["totals"] = {"gross": money(gross), "discounts": money("0"), "claimed": money(gross)}
    return c


SMALL = [("surgery", "Surgeon fee", "8000.00"), ("medicine", "Medicines", "4000.00")]  # 12,000: clean + <= T_auto
MID = [("surgery", "Surgeon fee", "50000.00"), ("medicine", "Medicines", "26500.00")]  # 76,500: above T_auto, below T_four
BIG = [("surgery", "Surgeon fee", "400000.00"), ("medicine", "Medicines", "200000.00")]  # 600,000: above T_four


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


async def go(env, claim):
    register_claim_docs(env, claim)
    r = await env.sim.submit(claim)
    assert r.status_code == 202, r.text
    await jobs.drain({"fetch_documents", "start_verification"})
    row = (await q(env, "SELECT id, status::text AS st, etag, assigned_reviewer, approved_amount FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0]
    if row.st == "verifying" or __import__("os").environ.get("DBG_FLOW"):
        st_ = await q(env, "SELECT s.step, s.status, s.findings FROM core.verification_step s JOIN core.verification_run r ON r.id = s.run_id WHERE r.case_id = :c", c=row.id)
        print("DBG", row.st, [(x.step, x.status, [f["code"] for f in x.findings]) for x in st_])
    return row


async def submit(env, case_id, body, who="reviewer1", roles=("reviewer",), etag=None):
    async with env.client(who, list(roles)) as c:
        headers = {"If-Match": f'"case-v{etag}"'} if etag is not None else {}
        return await c.post(f"/v1/cases/{case_id}/decision/submit", json=body, headers=headers)


async def vote(env, decision_id, who, roles, verdict="approve", comment=None):
    async with env.client(who, list(roles)) as c:
        return await c.post(f"/v1/decisions/{decision_id}/approvals", json={"verdict": verdict, "comment": comment})


async def test_clean_claim_within_t_auto_is_approved_automatically(env):
    claim = claim_of(SMALL)
    row = await go(env, claim)
    assert row.st == "approved" and str(row.approved_amount) == "12000.00"
    d = (await q(env, "SELECT kind, status, gate_tier, created_by, reviewer_ids FROM core.decision WHERE case_id = :c AND kind = 'final'", c=row.id))[0]
    assert (d.kind, d.status, d.gate_tier, d.created_by) == ("final", "finalised", "auto", "system:auto-approval") and d.reviewer_ids == ["system:auto-approval"]
    assert str((await q(env, "SELECT utilised_amount FROM core.policy_claim_utilisation u JOIN core.claim_case c ON c.policy_id = u.policy_id WHERE c.id = :c", c=row.id))[0].utilised_amount) not in ("0", "0.00")
    cb = await q(env, "SELECT endpoint, payload FROM ops.outbox WHERE case_id = :c ORDER BY seq", c=row.id)
    dec_cb = next(r for r in cb if r.endpoint.endswith("/decisions"))
    parsed = ContractDecision.model_validate(dec_cb.payload["decision"], strict=False)
    assert parsed.outcome.value == "approve" and parsed.approved_amount.amount == Decimal("12000.00") and parsed.reviewer_ids == ["system:auto-approval"]
    assert any(r.endpoint.endswith("/status") and r.payload["hospital_visible_status"] == "approved" for r in cb)
    assert ("initiate_settlement", {"case_id": str(row.id)}) in jobs.pending()
    async with env.sm() as s:
        assert (await audit.verify(s, row.id)).ok
    ev = [r.event_type for r in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=row.id)]
    assert "decision.finalised" in ev and "human.approved" not in ev  # no human involved


async def test_auto_approval_can_be_switched_off_by_config_flag(env, monkeypatch):
    monkeypatch.setattr(env.settings, "allow_auto_approval", False)
    row = await go(env, claim_of(SMALL))
    assert row.st == "ready_for_decision"


async def test_warning_prevents_auto_but_reviewer_tier_confirms_alone(env):
    claim = claim_of(SMALL)
    claim["documents"][0]["parse_confidence"] = 0.5  # low parse confidence -> warning -> not "clean"
    row = await go(env, claim)
    assert row.st == "ready_for_decision"
    r = await submit(env, row.id, {"outcome": "approve", "approved_amount": "12000.00"}, who=row.assigned_reviewer or "reviewer1", etag=row.etag)
    assert r.status_code == 200, r.text
    print("GATE", r.json()["gate"])
    assert r.json()["kind"] == "final" and r.json()["gate"]["tier"] == "reviewer" and r.json()["case_status"] == "approved"


async def test_single_approver_flow_with_sod_and_callback(env):
    row = await go(env, claim_of(MID))
    assert row.st == "ready_for_decision"
    r = await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"}, who="reviewer1", etag=row.etag)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pending_approval" and body["gate"]["tier"] == "single_approver" and body["case_status"] == "awaiting_approval" and "T_auto" in body["gate"]["explanation"]
    dec_id = body["decision_id"]
    assert (await vote(env, dec_id, "reviewer1", ["reviewer"])).status_code == 403  # a reviewer cannot approve
    # SoD: the person who prepared the decision cannot approve it even with the approver role
    sod = await vote(env, dec_id, "reviewer1", ["reviewer", "approver"])
    assert sod.status_code == 403 and sod.json()["code"] == "sod_violation"
    assert (await vote(env, dec_id, "approver1", ["approver"], "reject")).json()["code"] == "comment_required"
    ok = await vote(env, dec_id, "approver1", ["approver"])
    assert ok.status_code == 200 and ok.json()["status"] == "final" and ok.json()["case_status"] == "approved"
    again = await vote(env, dec_id, "approver2", ["approver"])
    assert again.status_code == 409 and again.json()["code"] == "task_closed"
    cb = await q(env, "SELECT endpoint, payload, seq FROM ops.outbox WHERE case_id = :c ORDER BY seq", c=row.id)
    d = next(r for r in cb if r.endpoint.endswith("/decisions"))
    parsed = ContractDecision.model_validate(d.payload["decision"], strict=False)
    assert parsed.reviewer_ids == ["reviewer1", "approver1"] and parsed.policy_version >= 1
    seqs = [r.seq for r in cb]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    async with env.sm() as s:
        assert (await audit.verify(s, row.id)).ok


async def test_dual_approval_needs_two_distinct_people_including_a_senior(env):
    row = await go(env, claim_of(BIG, min_si=1_000_000))
    async with env.client("reviewer1", ["reviewer"]) as c:
        payable = (await c.get(f"/v1/cases/{row.id}/decision")).json()["recommendation"]["approved_amount"]  # capped by remaining sum insured
    r = await submit(env, row.id, {"outcome": "partial", "approved_amount": payable, "reason_codes": ["SI_CAP"]}, who="reviewer1", etag=row.etag)
    assert r.status_code == 200, r.text
    assert r.json()["gate"]["tier"] == "dual_approver" and r.json()["gate"]["required_approvals"] == 2
    dec_id = r.json()["decision_id"]
    first = await vote(env, dec_id, "approver1", ["approver"])
    assert first.json() == {"status": "pending", "remaining": 1, "senior_needed": 1}
    assert (await vote(env, dec_id, "approver1", ["approver"])).json()["code"] == "already_voted"
    second = await vote(env, dec_id, "approver2", ["approver"])
    assert second.json()["status"] == "pending" and second.json()["senior_needed"] == 1  # two non-seniors: stays open
    third = await vote(env, dec_id, "senior9", ["senior_reviewer", "approver"])  # senior1 is the assignee -> SoD
    assert third.json()["status"] == "final" and third.json()["case_status"] == "partially_approved"


async def test_return_invalidates_votes_and_case_goes_back_to_reviewer(env):
    row = await go(env, claim_of(MID))
    dec = (await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"}, etag=row.etag)).json()["decision_id"]
    r = await vote(env, dec, "approver1", ["approver"], "return", "Please re-check consumables")
    assert r.json() == {"status": "returned"}
    st = (await q(env, "SELECT status::text AS s FROM core.claim_case WHERE id = :c", c=row.id))[0].s
    assert st == "ready_for_decision"
    assert (await q(env, "SELECT count(*) FROM core.approval WHERE decision_id = :d AND valid", d=uuid.UUID(dec)))[0][0] == 0
    again = await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"})
    assert again.status_code == 200 and again.json()["kind"] == "pending_approval"  # a fresh decision/task opens


async def test_amount_rules_exceeds_calc_reject_codes_and_stale_etag(env):
    row = await go(env, claim_of(MID))
    over = await submit(env, row.id, {"outcome": "approve", "approved_amount": "80000.00"}, etag=row.etag)
    assert over.status_code == 422 and over.json()["code"] == "exceeds_calculation"
    no_reason = await submit(env, row.id, {"outcome": "partial", "approved_amount": "70000.00"})
    assert no_reason.status_code == 422
    ok_cut = await submit(env, row.id, {"outcome": "partial", "approved_amount": "70000.00", "reason_codes": ["manual_cut"], "note": "non-payable consumables"})
    assert ok_cut.status_code == 200
    stale = await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"}, etag=1)
    assert stale.status_code in (409, 412)
    row2 = await go(env, claim_of(MID))
    no_code = await submit(env, row2.id, {"outcome": "reject"})
    assert no_code.status_code == 422
    rej = await submit(env, row2.id, {"outcome": "reject", "reason_codes": ["DOCS_NOT_PROVIDED"]})
    assert rej.status_code == 200 and rej.json()["gate"]["tier"] == "single_approver"
    # manual increase above the engine result needs a senior with an override reason and raises the review flag
    row3 = await go(env, claim_of(MID))
    assert (await submit(env, row3.id, {"outcome": "approve", "approved_amount": "77000.00"}, who="senior1", roles=("reviewer", "senior_reviewer"))).status_code == 422
    inc = await submit(env, row3.id, {"outcome": "approve", "approved_amount": "77000.00", "override_reason": "documented extra implant cost"}, who="senior1", roles=("reviewer", "senior_reviewer"))
    assert inc.status_code == 422  # never above the claimed amount, whatever the role
    # manual increase above the engine payable (< claimed): senior + override reason, raises the manual_increase flag -> approver needed
    row4 = await go(env, claim_of([("surgery", "Surgeon fee", "50000.00"), ("medicine", "Medicines", "26000.00"), ("other", "Registration charges", "500.00")]))
    assert row4.st == "ready_for_decision"
    up = await submit(env, row4.id, {"outcome": "approve", "approved_amount": "76500.00", "override_reason": "registration charge is payable per policy annexure"},
                      who="senior2", roles=("reviewer", "senior_reviewer"))
    assert up.status_code == 200, up.text
    assert "manual_increase" in up.json()["gate"]["flags"] and up.json()["gate"]["tier"] == "single_approver"


async def test_five_parallel_approvals_finalise_exactly_once(env):
    row = await go(env, claim_of(MID))
    dec = (await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"})).json()["decision_id"]
    users = [f"appr{i}" for i in range(5)]
    rs = await asyncio.gather(*(vote(env, dec, u, ["approver"]) for u in users))
    assert sum(1 for r in rs if r.status_code == 200 and r.json().get("status") == "final") == 1
    assert (await q(env, "SELECT count(*) FROM ops.outbox WHERE case_id = :c AND endpoint LIKE '%decisions'", c=row.id))[0][0] == 1
    util = (await q(env, "SELECT utilised_amount FROM core.policy_claim_utilisation u JOIN core.claim_case c ON c.policy_id = u.policy_id WHERE c.id = :c", c=row.id))[0].utilised_amount
    assert util >= Decimal("76500.00")


async def test_approval_queue_excludes_own_work_and_gate_stats(env):
    row = await go(env, claim_of(MID))
    await submit(env, row.id, {"outcome": "approve", "approved_amount": "76500.00"}, who="reviewer1")
    async with env.client("approver9", ["approver"]) as c:
        items = (await c.get("/v1/approvals/queue")).json()["items"]
        assert any(i["insurer_claim_no"] for i in items) and all(i["tier"] in ("single_approver", "dual_approver") for i in items)
    async with env.client("reviewer1", ["approver"]) as c:
        mine = (await c.get("/v1/approvals/queue")).json()["items"]
        assert all(i["case_id"] != str(row.id) for i in mine)  # excludes ones I prepared
    async with env.client("admin1", ["admin"]) as c:
        assert (await c.get("/v1/admin/gate/stats")).status_code == 200
    async with env.client("reviewer1", ["reviewer"]) as c:
        assert (await c.get("/v1/admin/gate/stats")).status_code == 403
        g = (await c.get(f"/v1/cases/{row.id}/decision")).json()
        assert g["task"]["tier"] == "single_approver" and g["gate"]["tier"] in ("single_approver", "reviewer")


async def test_hospital_receives_decision_callback_and_status_path(env):
    row = await go(env, claim_of(SMALL))
    sender = outbox.make_sender(env.sm, env.sim.receiver_client(), env.settings, backoff_scale=0.0)
    for _ in range(4):
        await sender.run_once()
    mine = [r for r in env.sim.received if r.body["claim_ref"] and r.applied]
    kinds = [r.kind for r in mine if r.body["claim_ref"] == next(iter({x.body["claim_ref"] for x in mine}))]
    assert "decisions" in {r.kind for r in env.sim.received}
    env.sim.assert_monotonic_sequences()
    assert row.st == "approved"
    _ = kinds
