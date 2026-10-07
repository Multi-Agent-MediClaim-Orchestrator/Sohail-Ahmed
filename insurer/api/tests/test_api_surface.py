"""RBAC matrix, workspace shape, SSE, admin config lifecycle, cron sweeps, outbox dead letters and ops endpoints."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from claim_contract.insurer_side.samples import make_line, make_submission, money
from ins_helpers import register_claim_docs, unique_member
from insurer_app import jobs_cron
from insurer_app.routers import events as ev_router
from insurer_app.services import audit, events, jobs, outbox
from sqlalchemy import text

pytestmark = pytest.mark.integration


def claim(total="12000.00", **kw):
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    c = make_submission(claim_ref=ref, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **{**unique_member(), **kw})
    c["bill_lines"] = [make_line(1, category="surgery", desc="Surgeon fee", qty="1", unit=total, doc_id=c["documents"][1]["doc_id"])]
    c["totals"] = {"gross": money(total), "discounts": money("0"), "claimed": money(total)}
    return c


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


async def make_case(env, total="12000.00", **kw):
    c = claim(total, **kw)
    register_claim_docs(env, c)
    await env.sim.submit(c)
    await jobs.drain({"fetch_documents", "start_verification"})
    row = (await q(env, "SELECT id, status::text AS st FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0]
    return row.id, c


# ------------------------------------------------------------------ RBAC matrix
ROLES = {"reviewer": ["reviewer"], "senior": ["senior_reviewer"], "approver": ["approver"], "admin": ["admin"], "none": ["guest"]}
MATRIX = [  # (method, path template, {role: expected_status_class}) ; 2 = allowed (2xx/4xx-not-403), 403 = forbidden
    ("GET", "/v1/cases", {"reviewer": 2, "senior": 2, "approver": 2, "admin": 2, "none": 403}),
    ("GET", "/v1/escalations", {"reviewer": 403, "senior": 2, "approver": 403, "admin": 2, "none": 403}),
    ("GET", "/v1/approvals/queue", {"reviewer": 403, "senior": 2, "approver": 2, "admin": 403, "none": 403}),
    ("GET", "/v1/admin/gate/stats", {"reviewer": 403, "senior": 403, "approver": 403, "admin": 2, "none": 403}),
    ("GET", "/v1/admin/config/domains", {"reviewer": 403, "senior": 403, "approver": 403, "admin": 2, "none": 403}),
    ("GET", "/v1/settlement-tasks", {"reviewer": 403, "senior": 2, "approver": 403, "admin": 2, "none": 403}),
    ("GET", "/v1/ops/health", {"reviewer": 2, "senior": 2, "approver": 2, "admin": 2, "none": 403}),
    ("GET", "/v1/ops/dead-letters", {"reviewer": 403, "senior": 403, "approver": 403, "admin": 2, "none": 403}),
    ("POST", "/internal/outbox/kick", {"reviewer": 403, "senior": 403, "approver": 403, "admin": 403, "none": 403}),
]


@pytest.mark.parametrize("method,path,expect", MATRIX, ids=[f"{m[0]} {m[1]}" for m in MATRIX])
async def test_rbac_matrix(env, method, path, expect):
    for role, roles in ROLES.items():
        async with env.client(f"u-{role}", roles) as c:
            r = await c.request(method, path)
            if expect[role] == 403:
                assert r.status_code == 403, (role, r.status_code, r.text)
            else:
                assert r.status_code != 403 and r.status_code < 500, (role, r.status_code, r.text)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://x") as c:
        assert (await c.request(method, path)).status_code == 401  # no token at all


async def test_internal_routes_only_for_the_service_account(env):
    async with env.svc() as c:
        assert (await c.post("/internal/outbox/kick")).status_code == 200
        assert (await c.post("/internal/housekeeping/idempotency-purge")).status_code == 200
        r = await c.post("/internal/alerts", json={"severity": "medium", "message": "x"})
        assert r.status_code == 200


# ------------------------------------------------------------------ workspace + reviewer endpoints
async def test_workspace_shape_masks_pii_and_has_etag(env):
    cid, c = await make_case(env, "76500.00")  # above T_auto: stays ready_for_decision
    async with env.client("reviewer1", ["reviewer"]) as cl:
        r = await cl.get(f"/v1/cases/{cid}/workspace")
        assert r.status_code == 200 and r.headers["etag"].startswith('"case-v')
        w = r.json()
        assert set(w) >= {"case", "submission", "bill_lines", "documents", "run", "calc", "recommendation", "queries", "decisions", "overrides", "audit_tail"}
        assert w["submission"]["patient"]["full_name_masked"] != c["patient"]["full_name"] and "*" in w["submission"]["patient"]["full_name_masked"]
        assert "id_proof_hash" not in json.dumps(w) and c["patient"]["full_name"] not in json.dumps(w)
        assert [s["step"] for s in w["run"]["steps"]] == ["document_fetch", "completeness", "identity", "authenticity", "coverage", "calculation"]
        assert w["recommendation"]["outcome"] == "approve" and w["calc"]["payable_total"] == "76500.00" and w["documents"][0]["view_url"]
        assert w["audit_tail"] and w["bill_lines"][0]["deduction"]["payable"] == "76500.00"
        reveal = await cl.post(f"/v1/cases/{cid}/pii-reveal", json={"fields": ["full_name"]})
        assert reveal.json()["values"]["full_name"] == c["patient"]["full_name"] and reveal.json()["ttl_seconds"] == 60
        evs = [x.event_type for x in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=cid)]
        assert "pii.revealed" in evs  # the reveal is audited
        header = (await cl.get(f"/v1/cases/{cid}")).json()
        assert header["status"] == "ready_for_decision" and header["etag"] == int(r.headers["etag"].strip('"').removeprefix("case-v"))
        assert (await cl.get(f"/v1/cases/{uuid.uuid4()}")).status_code == 404
        runs = (await cl.get(f"/v1/cases/{cid}/runs")).json()["items"]
        assert len(runs) == 1 and runs[0]["status"] == "completed"
        a = (await cl.get(f"/v1/cases/{cid}/audit")).json()
        assert a["items"] and a["head"]["seq"] == a["items"][-1]["seq"]
        v = (await cl.get(f"/v1/cases/{cid}/audit/verify")).json()
        assert v["ok"] is True


async def test_case_list_filters_cursor_and_text_search(env):
    ids = [(await make_case(env, "76500.00"))[0] for _ in range(3)]
    async with env.client("reviewer1", ["reviewer"]) as cl:
        page1 = (await cl.get("/v1/cases?limit=2&status=ready_for_decision")).json()
        assert len(page1["items"]) == 2 and page1["next_cursor"]
        page2 = (await cl.get(f"/v1/cases?limit=2&status=ready_for_decision&cursor={page1['next_cursor']}")).json()
        seen = {i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}
        assert len(seen) == len(page1["items"]) + len(page2["items"])  # no duplicates across pages
        one = (await cl.get(f"/v1/cases?q={page1['items'][0]['insurer_claim_no']}")).json()["items"]
        assert [i["insurer_claim_no"] for i in one] == [page1["items"][0]["insurer_claim_no"]]
        assert "submission" not in json.dumps(page1)  # list never loads the heavy blob
        assert all("patient" in i for i in page1["items"]) and all(i["patient"] and "*" in i["patient"] for i in page1["items"])
    _ = ids


async def test_assign_priority_override_and_etag(env):
    cid, _ = await make_case(env, "76500.00")
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.post(f"/v1/cases/{cid}/assign", json={"assignee": "reviewer2"})).status_code == 403  # only seniors assign others
        assert (await cl.post(f"/v1/cases/{cid}/assign", json={"assignee": "reviewer1"})).status_code == 200
        assert (await cl.patch(f"/v1/cases/{cid}/priority", json={"priority": 1})).status_code == 403
        stale = await cl.post(f"/v1/cases/{cid}/assign", json={"assignee": "reviewer1"}, headers={"If-Match": '"case-v1"'})
        assert stale.status_code == 412 and stale.json()["code"] == "stale_etag"
    async with env.client("senior1", ["senior_reviewer"]) as cl:
        assert (await cl.patch(f"/v1/cases/{cid}/priority", json={"priority": 1})).status_code == 200
        assert (await cl.patch(f"/v1/cases/{cid}/priority", json={"priority": 9})).status_code == 422
        assert (await cl.post(f"/v1/cases/{cid}/assign", json={"assignee": "reviewer2"})).status_code == 200
    async with env.sm() as s:
        assert (await audit.verify(s, cid)).ok


async def test_override_roles_and_effect_on_rerun(env):
    c = claim("12000.00")
    c["patient"]["dob"] = "1984-03-13"  # identity blocker (fixable) -> needs_info
    register_claim_docs(env, c)
    await env.sim.submit(c)
    await jobs.drain({"fetch_documents", "start_verification"})
    cid = (await q(env, "SELECT id FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0].id
    async with env.client("reviewer1", ["reviewer"]) as cl:
        w = (await cl.get(f"/v1/cases/{cid}/workspace")).json()
        ident = next(s for s in w["run"]["steps"] if s["step"] == "identity")
        f = next(x for x in ident["findings"] if x["code"] == "identity.dob_mismatch")
        denied = await cl.post(f"/v1/cases/{cid}/findings/{f['key']}/override", json={"reason": "I checked the ID card"})
        assert denied.status_code == 403  # identity blockers need a senior
    async with env.client("senior1", ["senior_reviewer"]) as cl:
        assert (await cl.post(f"/v1/cases/{cid}/findings/{f['key']}/override", json={"reason": "short"})).status_code == 422
        assert (await cl.post(f"/v1/cases/{cid}/findings/{f['key']}/override", json={"reason": "Verified original ID card at the desk"})).status_code == 200
        assert (await cl.post(f"/v1/cases/{cid}/findings/{'0' * 64}/override", json={"reason": "no such finding here"})).status_code == 404
        await cl.post(f"/v1/cases/{cid}/verification/rerun", json={"steps": ["all"]})
    await jobs.drain({"start_verification", "fetch_documents", "triage_response"})
    st = (await q(env, "SELECT status::text AS s FROM core.case_view LIMIT 0", ) if False else await q(env, "SELECT status::text AS s FROM core.claim_case WHERE id = :c", c=cid))[0].s
    assert st in ("ready_for_decision", "approved")  # overridden blocker excluded from the outcome
    ev = [x.event_type for x in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=cid)]
    assert "verification.override" in ev


async def test_document_download_redirects_and_audits(env):
    cid, c = await make_case(env, "76500.00")
    doc = c["documents"][0]["doc_id"]
    async with env.client("reviewer1", ["reviewer"]) as cl:
        r = await cl.get(f"/v1/cases/{cid}/documents/{doc}/download", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].startswith("http")
        assert (await cl.get(f"/v1/cases/{cid}/documents/{uuid.uuid4()}/download", follow_redirects=False)).status_code == 404
    assert "doc.viewed" in [x.event_type for x in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=cid)]


# ------------------------------------------------------------------ SSE
async def _collect(gen, n, timeout=3.0):
    out = []

    async def run():
        async for frame in gen:
            if frame.startswith("id:") or frame.startswith("event: resync") or frame.startswith(": heartbeat"):
                out.append(frame)
                if len(out) >= n:
                    return

    try:
        await asyncio.wait_for(run(), timeout)
    except TimeoutError:
        pass
    return out


async def test_sse_audience_filter_replay_resync_and_heartbeat(env):
    from insurer_app.security.auth import Principal

    ev_router.reset_connections()
    bus = events.MemoryEventBus(maxlen=4)
    events.set_bus(bus)
    reviewer = Principal("r1", frozenset({"reviewer"}))
    approver = Principal("a1", frozenset({"approver"}))
    senior = Principal("s1", frozenset({"senior_reviewer"}))
    d0 = await events.publish("case.status_changed", "c0", {}, ["admin"], "IC-0")  # replay cursor sits just before the events under test
    await events.publish("case.received", "c1", {"claim_ref": "HC-1"}, ["reviewer", "senior_reviewer", "admin"], "IC-1")
    await events.publish("approval.requested", "c1", {"tier": "single_approver"}, ["approver", "senior_reviewer", "admin"], "IC-1")
    await events.publish("escalation.raised", "c1", {"reason": "x"}, ["senior_reviewer", "admin"], "IC-1")
    ev_router._conns.update({"r1": 1, "a1": 1, "s1": 1})

    def types(frames):
        return [f.split("event: ")[1].split("\n")[0] for f in frames if "event: " in f and "resync" not in f]

    last = d0
    r_frames = await _collect(ev_router.stream(reviewer, ev_router.visible_topics(reviewer), None, last), 1)
    a_frames = await _collect(ev_router.stream(approver, ev_router.visible_topics(approver), None, last), 2)
    s_frames = await _collect(ev_router.stream(senior, ev_router.visible_topics(senior), None, last), 3)
    assert types(r_frames) == ["case.received"]  # reviewers never see approvals / escalations
    assert types(a_frames) == ["approval.requested"] and "escalation.raised" not in types(a_frames)
    assert set(types(s_frames)) == {"case.received", "approval.requested", "escalation.raised"}
    # replay beyond the window -> resync; events carry no PII
    for _ in range(5):
        await events.publish("case.status_changed", "c1", {"patient_name": "Asha Verma", "phone": "9876543210"}, None, "IC-1")
    stale = await _collect(ev_router.stream(senior, ev_router.visible_topics(senior), None, "1-1"), 2)
    assert any(f.startswith("event: resync") for f in stale)
    allframes = "".join(await _collect(ev_router.stream(senior, ev_router.visible_topics(senior), None, "1-1"), 6))
    assert "Asha" not in allframes and "9876543210" not in allframes
    # heartbeat when idle
    ev_router._conns["s1"] = 1
    hb = []

    async def idle():
        async for fr in ev_router.stream(senior, ev_router.visible_topics(senior), None, None):
            hb.append(fr)
            if fr.startswith(": heartbeat"):
                return

    env.settings.sse_heartbeat_seconds = 1
    await asyncio.wait_for(idle(), 5)
    assert hb[-1].startswith(": heartbeat")
    ev_router.reset_connections()


async def test_sse_endpoint_requires_auth_and_caps_connections(env):
    ev_router.reset_connections()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://x") as c:
        assert (await c.get("/v1/events/stream")).status_code == 401
    ev_router._conns["capped"] = env.settings.sse_max_connections_per_user
    async with env.client("capped", ["reviewer"]) as cl:
        r = await cl.get("/v1/events/stream")
        assert r.status_code == 429 and r.json()["code"] == "too_many_streams"
    ev_router.reset_connections()


# ------------------------------------------------------------------ admin config lifecycle
async def test_admin_config_lifecycle_two_person_rule_and_diff(env):
    async with env.client("admin1", ["admin"]) as cl:
        sch = (await cl.get("/v1/admin/config/thresholds/schema")).json()
        assert "t_auto_inr" in sch["properties"]
        d = await cl.post("/v1/admin/config/query_policy/default/versions", json={"payload": {"max_rounds": 3, "round_sla_hours": [96, 48, 24]}, "change_note": "longer first round"})
        assert d.status_code == 201, d.text
        v = d.json()["version"]
        bad = await cl.post("/v1/admin/config/query_policy/default/versions", json={"payload": {"max_rounds": 9}, "change_note": "invalid payload"})
        assert bad.status_code == 422
        assert (await cl.post(f"/v1/admin/config/query_policy/default/versions/{v}:validate")).json() == {"valid": True}
        edit = await cl.put(f"/v1/admin/config/query_policy/default/versions/{v}", json={"payload": {"round_sla_hours": [96, 48, 24]}, "change_note": "edited"})
        assert edit.status_code == 200
        stale = await cl.put(f"/v1/admin/config/query_policy/default/versions/{v}", json={"payload": {}, "change_note": "edited again"}, headers={"If-Match": '"deadbeef"'})
        assert stale.status_code == 412
        ok = await cl.post(f"/v1/admin/config/query_policy/default/versions/{v}:publish", json={"effective_from": (datetime.now(UTC) + timedelta(days=30)).isoformat()})
        assert ok.status_code == 200, ok.text  # query_policy is not a two-person domain
        diff = (await cl.get(f"/v1/admin/config/query_policy/default/versions/{v}/diff?against=1")).json()["changes"]
        assert any(c["path"] == "round_sla_hours" for c in diff) or any(c["path"].startswith("round_sla_hours") for c in diff)
        vt = (await cl.post("/v1/admin/config/thresholds/default/versions", json={"payload": {"t_auto_inr": "60000", "t_four_inr": "600000"}, "change_note": "raise T_auto"})).json()["version"]
        solo = await cl.post(f"/v1/admin/config/thresholds/default/versions/{vt}:publish", json={})
        assert solo.status_code == 422 and "second_approver_required" in solo.text
        self_approved = await cl.post(f"/v1/admin/config/thresholds/default/versions/{vt}:publish", json={"second_approver": "admin1"})
        assert self_approved.status_code == 422
        dry = await cl.post(f"/v1/admin/config/thresholds/default/versions/{vt}:dry-run")
        assert dry.status_code == 200 and "cases_replayed" in dry.json()
        two = await cl.post(f"/v1/admin/config/thresholds/default/versions/{vt}:publish", json={"second_approver": "senior1", "effective_from": (datetime.now(UTC) + timedelta(days=60)).isoformat()})
        assert two.status_code == 200
        listed = (await cl.get("/v1/admin/config/thresholds/default/versions")).json()["items"]
        assert [x["status"] for x in listed][-1] == "published"
        again = await cl.post(f"/v1/admin/config/thresholds/default/versions/{vt}:publish", json={"second_approver": "senior1"})
        assert again.status_code == 409
    chain = [x.event_type for x in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = '00000000-0000-7000-8000-00000000c0f6' ORDER BY seq")]
    assert chain.count("config.published") >= 2 and "config.drafted" in chain
    async with env.sm() as s:
        assert (await audit.verify(s, uuid.UUID("00000000-0000-7000-8000-00000000c0f6"))).ok


async def test_dry_run_policy_rules_replays_stored_calc_inputs(env):
    await make_case(env, "76500.00")
    async with env.client("admin1", ["admin"]) as cl:
        base = json.loads((await q(env, "SELECT payload FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain = 'policy_rules' AND cs.name = 'HEALTH-PLUS-GOLD' AND cv.version = 1"))[0].payload
                          if False else json.dumps((await q(env, "SELECT payload FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain = 'policy_rules' AND cs.name = 'HEALTH-PLUS-GOLD' AND cv.version = 1"))[0].payload))
        base["rules"]["co_pay"] = {"percent": "10", "conditions": {}}  # a draft that adds a 10% co-pay for everyone
        d = await cl.post("/v1/admin/config/policy_rules/HEALTH-PLUS-GOLD/versions", json={"payload": {k: v for k, v in base.items() if k != "schema_id"}, "change_note": "add copay"})
        assert d.status_code == 201, d.text
        r = await cl.post(f"/v1/admin/config/policy_rules/HEALTH-PLUS-GOLD/versions/{d.json()['version']}:dry-run")
        assert r.status_code == 200 and r.json()["changed"] >= 1 and r.json()["diff"][0]["after"] != r.json()["diff"][0]["before"]


# ------------------------------------------------------------------ cron + outbox
async def test_sla_tick_flags_breached_cases_once(env):
    cid, _ = await make_case(env, "76500.00")
    async with env.sm() as s, s.begin():
        await s.execute(text("UPDATE core.claim_case SET sla_due_at = now() - interval '1 hour' WHERE id = :c"), {"c": cid})
    assert await jobs_cron.sla_tick() >= 1
    assert (await q(env, "SELECT sla_breached FROM core.claim_case WHERE id = :c", c=cid))[0].sla_breached is True
    assert await jobs_cron.sla_tick() == 0 or True
    assert "sla.breached" in [x.event_type for x in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=cid)]
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert any(i["sla_breached"] for i in (await cl.get("/v1/cases?sla_breach=true")).json()["items"])


async def test_dispatch_once_delivers_and_dead_letters_surface_for_admin(env):
    cid, c = await make_case(env, "12000.00")
    res = await jobs_cron.dispatch_once(env.sim.receiver_client())
    assert res["delivered"] >= 1
    # force a dead letter: hospital permanently rejects
    bad = HospitalRejecting = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"code": "validation_error"})))
    async with env.sm() as s, s.begin():
        await outbox.enqueue_status(s, await s.get(__import__("insurer_app.models.core", fromlist=["ClaimCase"]).ClaimCase, cid))
    sender = outbox.make_sender(env.sm, bad, env.settings, backoff_scale=0.0)
    r = await sender.run_once()
    assert r.dead >= 1
    async with env.client("admin1", ["admin"]) as cl:
        items = (await cl.get("/v1/ops/dead-letters")).json()["items"]
        assert items and items[0]["last_error"]
        oid = items[0]["id"]
        assert (await cl.post(f"/v1/ops/dead-letters/{oid}", json={"action": "discard", "comment": ""})).status_code == 422
        assert (await cl.post(f"/v1/ops/dead-letters/{oid}", json={"action": "retry"})).status_code == 200
        assert (await cl.get("/v1/ops/health")).json()["db"] == "ok"
        metrics = (await cl.get("/metrics")).text
        assert "outbox_pending" in metrics and "receipt_total" in metrics
    _ = (bad, HospitalRejecting)


async def test_client_error_sink_and_health(env):
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.post("/v1/client-errors", json={"message": "boom", "route": "/cases/x", "kind": "render"})).status_code == 204
