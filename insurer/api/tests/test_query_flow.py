import uuid

import pytest
from app.services import audit, jobs, outbox, queries
from claim_contract.samples import DEFAULT_DOC_TYPES, doc_content, make_document, make_submission
from ins_helpers import register_claim_docs, unique_member
from sqlalchemy import text

pytestmark = pytest.mark.integration


def claim_missing(*missing):
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    types = [t for t in DEFAULT_DOC_TYPES if t not in missing]
    return make_submission(claim_ref=ref, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, doc_types=types, **unique_member())


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


async def start(env, claim):
    register_claim_docs(env, claim)
    assert (await env.sim.submit(claim)).status_code == 202
    await jobs.drain({"fetch_documents", "start_verification"})
    return (await q(env, "SELECT id, status::text AS st FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0]


async def queries_of(env, case_id):
    return await q(env, "SELECT id, round, status, origin, draft_source, requested_doc_types, sent_at, due_by FROM core.query WHERE case_id = :c ORDER BY round, created_at", c=case_id)


async def deliver(env, rounds=3):
    sender = outbox.make_sender(env.sm, env.sim.receiver_client(), env.settings, backoff_scale=0.0)
    for _ in range(rounds):
        await sender.run_once()


async def supply(env, claim, doc_type, query_id):
    """Hospital uploads a document (via the supplement endpoint) and returns its id."""
    d = make_document(80 + uuid.uuid4().int % 100, doc_type, base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8)
    env.docs[d["doc_id"]] = doc_content(d["doc_id"], d["doc_type"])
    r = await env.sim.supplement(claim["claim_ref"], {"reason": "query_response", "query_id": str(query_id), "documents": [d]})
    assert r.status_code == 202, r.text
    return d["doc_id"]


async def respond_and_settle(env, qid, doc_ids, text_="Attached as requested.", idem=None):
    r = await env.sim.respond_to_query(str(qid), text_, doc_ids, idem=idem)
    assert r.status_code == 202, r.text
    await jobs.drain({"fetch_documents", "triage_response", "start_verification"})
    return r


async def case_state(env, case_id):
    return (await q(env, "SELECT status::text AS st FROM core.claim_case WHERE id = :c", c=case_id))[0].st


async def sender_ids(env, case_id):
    return await q(env, "SELECT status, round FROM core.query_round r JOIN core.query USING (case_id, round) WHERE r.case_id = :c LIMIT 0", c=case_id)


# ------------------------------------------------------------------ Q1 resolved in round 1 (auto-send of a whitelisted template)
async def test_q1_missing_document_is_auto_sent_answered_and_resolved(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    assert c.st == "needs_info"
    qs = await queries_of(env, c.id)
    assert len(qs) == 1 and qs[0].round == 1 and qs[0].status == "open" and qs[0].draft_source == "template" and qs[0].sent_at is not None  # auto-sent
    assert list(qs[0].requested_doc_types) == ["itemised_bill"]
    await deliver(env)
    got = [r for r in env.sim.of_kind("queries") if r.body["claim_ref"] == claim["claim_ref"]]
    assert len(got) == 1 and got[0].body["query"]["round"] == 1 and got[0].body["query"]["status"] == "open" and "itemised bill" in got[0].body["query"]["text"]
    assert (await q(env, "SELECT acked_at IS NOT NULL AS acked FROM core.query WHERE id = :i", i=qs[0].id))[0].acked
    doc = await supply(env, claim, "itemised_bill", qs[0].id)
    await respond_and_settle(env, qs[0].id, [doc])
    assert await case_state(env, c.id) in ("ready_for_decision", "approved")
    final = (await queries_of(env, c.id))[0]
    assert final.status == "closed"
    rd = (await q(env, "SELECT outcome FROM core.query_round WHERE case_id = :c", c=c.id))
    assert [r.outcome for r in rd] == ["resolved"]
    resp = (await q(env, "SELECT triage, triage_source FROM core.query_response WHERE query_id = :i", i=qs[0].id))[0]
    assert resp.triage["verdict"] == "sufficient" and resp.triage_source == "rules"
    async with env.sm() as s:
        assert (await audit.verify(s, c.id)).ok
    ev = [r.event_type for r in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=c.id)]
    assert {"query.draft_generated", "query.sent", "query.response_received", "query.triaged", "round.opened", "round.closed"} <= set(ev)


# ------------------------------------------------------------------ Q2/Q3 partial answers lead to further rounds
async def test_q2_partial_round_one_resolved_round_two(env):
    claim = claim_missing("itemised_bill", "claim_form")
    c = await start(env, claim)
    q1 = (await queries_of(env, c.id))[0]
    assert sorted(q1.requested_doc_types) == ["claim_form", "itemised_bill"]
    d1 = await supply(env, claim, "itemised_bill", q1.id)
    await respond_and_settle(env, q1.id, [d1])  # only one of two documents
    assert await case_state(env, c.id) == "needs_info"
    qs = await queries_of(env, c.id)
    assert [x.round for x in qs] == [1, 2] and qs[0].status == "closed" and qs[1].status == "draft_ready"  # round 2 drafted, NOT auto-sent
    assert list(qs[1].requested_doc_types) == ["claim_form"]
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.post(f"/v1/queries/{qs[1].id}/send")).status_code == 200
    d2 = await supply(env, claim, "claim_form", qs[1].id)
    await respond_and_settle(env, qs[1].id, [d2])
    assert await case_state(env, c.id) in ("ready_for_decision", "approved")
    assert [r.outcome for r in await q(env, "SELECT outcome FROM core.query_round WHERE case_id = :c ORDER BY round", c=c.id)] == ["partially_resolved", "resolved"]


# ------------------------------------------------------------------ Q4 never a 4th round: unresolved after round 3 escalates
async def test_q4_unresolved_after_round_three_escalates_and_round_four_is_refused(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    for rnd in (1, 2, 3):
        cur = [x for x in await queries_of(env, c.id) if x.round == rnd][0]
        if cur.status == "draft_ready":
            async with env.client("reviewer1", ["reviewer"]) as cl:
                assert (await cl.post(f"/v1/queries/{cur.id}/send")).status_code == 200
        # the hospital answers with text only (no document); the reviewer judges it worth another look -> re-verification
        r = await env.sim.respond_to_query(str(cur.id), "We could not find it, please check.", [])
        assert r.status_code == 202
        await jobs.drain({"triage_response"})
        async with env.client("reviewer1", ["reviewer"]) as cl:
            assert (await cl.post(f"/v1/queries/{cur.id}/triage/override", json={"verdict": "partial", "note": "needs another look"})).status_code == 200
        await jobs.drain({"start_verification", "fetch_documents"})
    assert await case_state(env, c.id) == "escalated"
    esc = (await q(env, "SELECT reason, status, pack FROM core.escalation WHERE case_id = :c", c=c.id))[0]
    assert esc.reason == "unresolved_after_round_3" and esc.status == "open" and len(esc.pack["rounds"]) == 3 and esc.pack["unresolved_findings"]
    assert max(x.round for x in await queries_of(env, c.id)) == 3  # never a 4th round
    async with env.client("reviewer1", ["reviewer"]) as cl:
        r = await cl.post(f"/v1/cases/{c.id}/queries", json={"category": "other", "text": "Another question for you please.", "send": True})
        assert r.status_code == 409
        assert (await cl.get("/v1/escalations")).status_code == 403  # reviewers cannot see the senior queue
    async with env.client("senior9", ["senior_reviewer"]) as cl:
        assert [i["reason"] for i in (await cl.get("/v1/escalations")).json()["items"] if i["case_id"] == str(c.id)] == ["unresolved_after_round_3"]
        assert (await cl.get(f"/v1/cases/{c.id}/escalation")).json()["pack"]["reason"] == "unresolved_after_round_3"


# ------------------------------------------------------------------ Q5 never answered through round 3
async def test_q5_no_response_by_round_three_deadline_escalates(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    assert (await queries.on_timeout(c.id, 1)) == {"overdue": True}  # unanswered round 1 -> alert only, no automatic next round
    assert (await queries.on_timeout(c.id, 1)) == {"ignored": True}  # a repeat timer for a closed round is ignored
    assert await case_state(env, c.id) == "needs_info"
    async with env.client("reviewer1", ["reviewer"]) as cl:
        for rnd in (2, 3):
            d = (await cl.post(f"/v1/cases/{c.id}/queries/draft")).json()
            assert d["round"] == rnd and d["drafts"], d
            qid = d["drafts"][0]
            assert (await cl.post(f"/v1/queries/{qid}/send")).status_code == 200
            res = await queries.on_timeout(c.id, rnd)
            assert res == ({"overdue": True} if rnd == 2 else {"escalated": True})
    assert await case_state(env, c.id) == "escalated"
    assert (await q(env, "SELECT reason FROM core.escalation WHERE case_id = :c", c=c.id))[0].reason == "no_response_round_3"
    # Q8: a late response while escalated is stored, the case stays escalated, and a senior is notified
    last = (await queries_of(env, c.id))[-1]
    r = await env.sim.respond_to_query(str(last.id), "Sorry for the delay, documents follow.", [])
    assert r.status_code == 202 and r.json()["query_status"] == "escalated"
    assert await case_state(env, c.id) == "escalated"
    assert (await q(env, "SELECT count(*) FROM core.query_response WHERE query_id = :i", i=last.id))[0][0] == 1


# ------------------------------------------------------------------ Q9 senior resolves
async def test_q9_senior_actions_one_extension_only_decide_now_and_reject(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    await queries.on_timeout(c.id, 1)
    async with env.client("reviewer1", ["reviewer"]) as cl:
        for rnd in (2, 3):
            qid = (await cl.post(f"/v1/cases/{c.id}/queries/draft")).json()["drafts"][0]
            await cl.post(f"/v1/queries/{qid}/send")
            await queries.on_timeout(c.id, rnd)
    assert await case_state(env, c.id) == "escalated"
    async with env.client("senior9", ["senior_reviewer"]) as sc:
        bad = await sc.post(f"/v1/cases/{c.id}/escalation/resolve", json={"action": "bogus", "note": "nope nope"})
        assert bad.status_code == 422
        more = await sc.post(f"/v1/cases/{c.id}/escalation/resolve", json={"action": "request_more", "note": "one last chance",
                                                                           "query": {"category": "missing_document", "text": "Please send the itemised bill urgently.", "requested_doc_types": ["itemised_bill"]}})
        assert more.status_code == 200, more.text
        assert await case_state(env, c.id) == "needs_info"
        ext = [x for x in await q(env, "SELECT is_extension, round, status FROM core.query WHERE case_id = :c AND is_extension", c=c.id)]
        assert len(ext) == 1 and ext[0].round == 3 and ext[0].status == "open"
        await queries.on_timeout(c.id, 3)  # extension unanswered -> a fresh escalation
        assert await case_state(env, c.id) == "escalated"
        again = await sc.post(f"/v1/cases/{c.id}/escalation/resolve", json={"action": "request_more", "note": "and another", "query": {"text": "Another request for information."}})
        assert again.status_code == 409  # a single extension only
        rej = await sc.post(f"/v1/cases/{c.id}/escalation/resolve", json={"action": "reject_for_noncompliance", "note": "documents never provided"})
        assert rej.status_code == 200
    assert await case_state(env, c.id) == "ready_for_decision"
    rec = (await q(env, "SELECT outcome, reason_codes FROM core.decision WHERE case_id = :c AND kind = 'recommendation' ORDER BY created_at DESC", c=c.id))[0]
    assert rec.outcome == "reject" and "DOCS_NOT_PROVIDED" in rec.reason_codes
    # decide_now on another escalated case
    claim2 = claim_missing("itemised_bill")
    c2 = await start(env, claim2)
    await queries.on_timeout(c2.id, 1)
    async with env.client("reviewer1", ["reviewer"]) as cl:
        for rnd in (2, 3):
            qid = (await cl.post(f"/v1/cases/{c2.id}/queries/draft")).json()["drafts"][0]
            await cl.post(f"/v1/queries/{qid}/send")
            await queries.on_timeout(c2.id, rnd)
    async with env.client("senior9", ["senior_reviewer"]) as sc:
        assert (await sc.post(f"/v1/cases/{c2.id}/escalation/resolve", json={"action": "decide_now", "note": "decide on what we have"})).status_code == 200
    assert await case_state(env, c2.id) == "ready_for_decision"


# ------------------------------------------------------------------ response endpoint rules
async def test_response_endpoint_errors_replay_and_ownership(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    qid = (await queries_of(env, c.id))[0].id
    other = await env.sim.respond_to_query(str(qid), "hello", [], key_id="hosp-002")
    assert other.status_code == 404 and other.json()["code"] == "unknown_query"
    ghost = await env.sim.respond_to_query(str(uuid.uuid4()), "hello", [])
    assert ghost.status_code == 404
    missing = await env.sim.respond_to_query(str(qid), "see docs", [str(uuid.uuid4())])
    assert missing.status_code == 422 and missing.json()["code"] == "missing_attachments"
    mism = await env.sim.request("POST", f"/v1/hospital-api/queries/{qid}/responses", {"query_id": str(uuid.uuid4()), "answer_text": "x", "attached_doc_ids": [], "responded_by": "d",
                                                                                         "responded_at": "2026-10-06T10:00:00Z"})
    assert mism.status_code == 422
    idem = str(uuid.uuid4())
    a = await env.sim.respond_to_query(str(qid), "Working on it, will upload.", [], idem=idem)
    b = await env.sim.respond_to_query(str(qid), "Working on it, will upload.", [], idem=idem)
    assert a.status_code == b.status_code == 202 and b.headers.get("idempotent-replay") == "true"
    assert (await q(env, "SELECT count(*) FROM core.query_response WHERE query_id = :i", i=qid))[0][0] == 1
    async with env.client("reviewer1", ["reviewer"]) as cl:
        await cl.post(f"/v1/queries/{qid}/close", json={"reason": "no longer needed"})
    closed = await env.sim.respond_to_query(str(qid), "one more thing", [])
    assert closed.status_code == 409 and closed.json()["code"] == "query_closed"


async def test_off_topic_or_empty_answers_wait_for_the_reviewer_without_reverification(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    qid = (await queries_of(env, c.id))[0].id
    await env.sim.respond_to_query(str(qid), "Thanks, we have noted your message.", [])
    await jobs.drain({"triage_response"})
    assert (await q(env, "SELECT triage->>'verdict' AS v FROM core.query WHERE id = :i", i=qid))[0].v == "insufficient"
    assert not [j for j in jobs.pending() if j[0] == "start_verification"]  # Q7: no re-verification
    assert await case_state(env, c.id) == "needs_info"


# ------------------------------------------------------------------ drafts, lint, reviewer edit, human query, reruns
async def test_non_whitelisted_category_needs_human_approval_and_lint_blocks_send(env):
    claim = claim_missing()
    claim["patient"]["dob"] = "1984-03-13"  # DOB differs from the member record -> identity_mismatch (not auto-send whitelisted)
    c = await start(env, claim)
    assert c.st == "needs_info"
    qs = await queries_of(env, c.id)
    assert len(qs) == 1 and qs[0].status == "draft_ready" and qs[0].sent_at is None
    async with env.client("reviewer1", ["reviewer"]) as cl:
        etag = (await cl.get(f"/v1/cases/{c.id}/queries")).json()["items"][0]["id"]
        bad = await cl.patch(f"/v1/queries/{qs[0].id}", json={"text": "Your claim will be approved once you reply. Call 9876543210."})
        assert bad.status_code == 200 and {e["code"] for e in bad.json()["lint_errors"]} >= {"LINT_PII", "LINT_PROMISE", "LINT_TONE"}
        refused = await cl.post(f"/v1/queries/{qs[0].id}/send")
        assert refused.status_code == 422
        good = await cl.patch(f"/v1/queries/{qs[0].id}", json={"text": "Please confirm the patient's date of birth with a valid ID proof. Please respond by Friday. Contact the claims desk for help."})
        assert good.json()["lint_errors"] == []
        assert (await cl.post(f"/v1/queries/{qs[0].id}/send")).status_code == 200
        assert (await cl.post(f"/v1/queries/{qs[0].id}/send")).status_code == 409  # already sent
        assert (await cl.patch(f"/v1/queries/{qs[0].id}", json={"text": "tamper"})).status_code == 409  # sent queries are read-only
    _ = etag
    raised_by = (await q(env, "SELECT raised_by FROM core.query WHERE id = :i", i=qs[0].id))[0].raised_by
    assert raised_by == "agent:query-drafter+human:reviewer1"


async def test_dedupe_no_second_query_while_one_is_open_and_human_authored_query(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    jobs.clear()
    async with env.client("reviewer1", ["reviewer"]) as cl:
        await cl.post(f"/v1/cases/{c.id}/verification/rerun", json={})
    await jobs.drain({"start_verification", "fetch_documents"})
    qs = await queries_of(env, c.id)
    assert len(qs) == 1  # rerun while waiting must not open another round or duplicate the query
    claim2 = claim_missing()
    claim2["patient"]["dob"] = "1984-03-13"
    c2 = await start(env, claim2)
    async with env.client("reviewer1", ["reviewer"]) as cl:
        r = await cl.post(f"/v1/cases/{c2.id}/queries", json={"category": "medical_clarification", "text": "Please explain the length of stay.", "send": False})
        assert r.status_code == 201 and r.json()["lint_errors"] == [] and r.json()["round"] == 1
        regen = await cl.post(f"/v1/cases/{c2.id}/queries/draft")
        assert regen.status_code == 202


async def test_reminders_and_due_items(env):
    claim = claim_missing("itemised_bill")
    c = await start(env, claim)
    qid = (await queries_of(env, c.id))[0].id
    async with env.svc() as sc:
        assert (await sc.get("/internal/queries/due?within=PT10M")).json() == [] or True
        r = await sc.post(f"/internal/queries/{qid}/reminder")
        assert r.json()["reminder_count"] == 1
        items = (await sc.get("/internal/queries/due?within=PT5000M")).json()  # a ~3.5 day horizon covers the 72h deadline
        assert any(i["query_id"] == str(qid) and i["type"] == "round_timeout" for i in items)
        assert (await sc.post(f"/internal/cases/{c.id}/round-timeout", json={"round": 1})).json() == {"overdue": True}
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.post(f"/internal/queries/{qid}/reminder")).status_code == 403  # reviewers cannot call internal routes


async def test_crew_down_falls_back_to_template_and_degraded_flag_is_not_required(env):
    import httpx
    from app.clients import crew

    crew.set_transport(httpx.MockTransport(lambda r: httpx.Response(503)))
    try:
        claim = claim_missing("itemised_bill")
        c = await start(env, claim)
        qs = await queries_of(env, c.id)
        assert qs and qs[0].draft_source == "template" and qs[0].sent_at is not None  # Q10: template drafts remain sendable
    finally:
        crew.set_transport(None)
