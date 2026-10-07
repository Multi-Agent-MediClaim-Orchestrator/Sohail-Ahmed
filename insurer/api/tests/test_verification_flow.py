import uuid

import pytest
from app.services import jobs
from claim_contract.samples import make_line, make_submission
from ins_helpers import register_claim_docs, unique_stay
from sqlalchemy import text

pytestmark = pytest.mark.integration


def fresh(**kw):
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    kw = {**unique_stay(), **kw}
    return make_submission(claim_ref=ref, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **kw)


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


async def run_pipeline(env, claim):
    register_claim_docs(env, claim)
    r = await env.sim.submit(claim)
    assert r.status_code == 202, r.text
    await jobs.drain()
    return (await q(env, "SELECT id, status::text AS st, recommended_amount, degraded FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0]


async def steps(env, case_id):
    rows = await q(env, "SELECT s.step, s.status, s.findings FROM core.verification_step s JOIN core.verification_run r ON r.id = s.run_id "
                        "WHERE r.case_id = :c ORDER BY r.run_no, s.step", c=case_id)
    return {r.step: r for r in rows}


async def test_clean_claim_reaches_ready_for_decision_with_full_recommendation(env):
    c = await run_pipeline(env, fresh())
    assert c.st == "ready_for_decision"
    rec = (await q(env, "SELECT outcome, approved_amount, calc_result_id FROM core.decision WHERE case_id = :c AND kind = 'recommendation'", c=c.id))[0]
    assert rec.outcome == "approve" and str(rec.approved_amount) == "76500.00" and rec.calc_result_id is not None
    st = await steps(env, c.id)
    assert {k: v.status for k, v in st.items()} == {"document_fetch": "passed", "completeness": "passed", "identity": "passed", "authenticity": "passed",
                                                     "coverage": "passed", "calculation": "passed"}
    # audit chain contains run + every step and verifies
    from app.services import audit

    async with env.sm() as s:
        assert (await audit.verify(s, c.id)).ok
    ev = [r.event_type for r in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c ORDER BY seq", c=c.id)]
    assert ev.count("verification.step.completed") == 6 and "verification.run.completed" in ev and "decision.recommended" in ev


async def test_missing_required_document_goes_to_needs_info_and_hospital_sees_under_query(env):
    claim = fresh(doc_types=["discharge_summary", "final_bill", "claim_form", "id_proof", "policy_card"])  # no itemised_bill
    c = await run_pipeline(env, claim)
    assert c.st == "needs_info"
    st = await steps(env, c.id)
    assert st["completeness"].status == "flagged"
    assert st["identity"].status == "skipped" and st["calculation"].status == "skipped"  # prerequisites did not pass
    codes = [f["code"] for f in st["completeness"].findings]
    assert codes == ["completeness.missing_required"] and st["completeness"].findings[0]["suggested_doc_types"] == ["itemised_bill"]
    cb = (await q(env, "SELECT payload->>'hospital_visible_status' AS hs FROM ops.outbox WHERE case_id = :c AND endpoint LIKE '%/status' ORDER BY seq", c=c.id))
    assert [r.hs for r in cb] == ["acknowledged", "under_query"]


async def test_unknown_policy_fails_identity_but_stays_fixable(env):
    c = await run_pipeline(env, fresh(policy_number="POL-UNKNOWN-9", member_id="MEM-00000009"))
    assert c.st == "needs_info"
    st = await steps(env, c.id)
    assert st["identity"].status == "failed" and st["identity"].findings[0]["code"] == "identity.policy_not_found" and st["identity"].findings[0]["fixable"]
    assert st["coverage"].status == "skipped"


async def test_overlapping_claim_is_unfixable_duplicate_and_recommends_reject(env):
    stay = unique_stay()
    first = fresh(**stay)
    await run_pipeline(env, first)
    second = fresh(**stay)  # same member, same hospital, same stay, different claim_ref
    c = await run_pipeline(env, second)
    st = await steps(env, c.id)
    assert any(f["code"] == "auth.duplicate_claim" for f in st["authenticity"].findings) and st["authenticity"].status == "failed"
    assert c.st == "ready_for_decision"
    rec = (await q(env, "SELECT outcome, reason_codes, approved_amount FROM core.decision WHERE case_id = :c AND kind = 'recommendation'", c=c.id))[0]
    assert rec.outcome == "reject" and "auth.duplicate_claim" in rec.reason_codes and str(rec.approved_amount) == "0.00"


async def test_stamp_missing_on_final_bill_is_fixable_blocker(env):
    claim = fresh()
    register_claim_docs(env, claim)
    await env.sim.submit(claim)
    await jobs.drain({"fetch_documents"})
    async with env.sm() as s, s.begin():  # vision-service results land on the documents
        await s.execute(text("UPDATE core.claim_document SET vision = CAST(:v AS JSONB) WHERE doc_type = 'final_bill' AND case_id = (SELECT id FROM core.claim_case WHERE hospital_claim_ref = :r)"),
                        {"v": '{"stamp_detected": false, "signature_detected": true, "tamper_score": 0.01}', "r": claim["claim_ref"]})
    await jobs.drain()
    c = (await q(env, "SELECT id, status::text AS st FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0]
    st = await steps(env, c.id)
    assert c.st == "needs_info", {k: (v.status, [f['code'] for f in v.findings]) for k, v in st.items()}
    assert [f["code"] for f in st["authenticity"].findings] == ["auth.stamp_missing"] and st["authenticity"].status == "flagged"


async def test_waiting_period_group_block_results_in_partial_recommendation(env):
    # knee replacement dx within 730d specific waiting: warning at coverage; the engine disallows the group's lines
    claim = fresh(diagnosis_codes=["M17.1"], lines=[
        make_line(1, category="surgery", desc="Surgeon fee knee", qty="1", unit="100000.00", doc_id=None),
        make_line(2, category="medicine", desc="Medicines", qty="1", unit="10000.00", doc_id=None)])
    for ln in claim["bill_lines"]:
        ln["source_doc_id"] = claim["documents"][1]["doc_id"]
    c = await run_pipeline(env, claim)
    rec = (await q(env, "SELECT outcome, approved_amount FROM core.decision WHERE case_id = :c AND kind = 'recommendation'", c=c.id))[0]
    st_ = {k: (v.status, [f['code'] for f in v.findings]) for k, v in (await steps(env, c.id)).items()}
    assert rec.outcome == "partial" and str(rec.approved_amount) == "10000.00", (st_, rec)


async def test_start_run_is_idempotent_by_client_token_and_one_run_at_a_time(env):
    from app.services import verification
    from app.verification.schemas import RunStart
    from claim_contract.errors import ProblemError

    claim = fresh()
    register_claim_docs(env, claim)
    await env.sim.submit(claim)
    await jobs.drain({"fetch_documents"})
    jobs.clear()
    cid = (await q(env, "SELECT id FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0].id
    a = await verification.start_run(env.config, cid, RunStart(trigger="initial", client_token="tok-1"))
    b = await verification.start_run(env.config, cid, RunStart(trigger="initial", client_token="tok-1"))
    assert a["run_id"] == b["run_id"] and b["existing"] is True
    with pytest.raises(ProblemError) as ei:
        await verification.start_run(env.config, cid, RunStart(trigger="manual_rerun", client_token="tok-2"))
    assert ei.value.code == "run_in_progress"
    # out-of-order step result is refused
    with pytest.raises(ProblemError) as ei:
        await verification.record_step(env.config, uuid.UUID(a["run_id"]), "identity", None)
    assert ei.value.code == "step_out_of_order"
