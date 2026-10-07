"""Claim builder, edit, sign-off, submission, outbox worker and callbacks (doc 06 §9)."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.services import audit as audit_svc
from tests.claims_helpers import crew_body, ready_case
from tests.test_documents import sql

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _reset_sim(capp: Any) -> None:
    """Scripted insurer failures and the per-key rate-limit counter must not leak between tests."""
    capp.state.sim.fail_next, capp.state.sim.reject_code = 0, None
    for k in [k async for k in capp.state.redis.scan_iter("rl:hosp:ins-001:*")]:
        await capp.state.redis.delete(k)


async def status_of(c: httpx.AsyncClient, tok: Any, case_id: str) -> str:
    return (await c.get(f"/v1/cases/{case_id}", headers=tok("officer1"))).json()["status"]  # type: ignore[no-any-return]


async def build(c: httpx.AsyncClient, tok: Any, capp: Any, case: dict[str, Any]) -> str:
    r = await c.post(f"/v1/cases/{case['id']}/claim/build", headers=tok("desk1"))
    assert r.status_code == 202, r.text
    return r.json()["job_id"]  # type: ignore[no-any-return]


async def to_review(
    c: httpx.AsyncClient, tok: Any, capp: Any, case: dict[str, Any], **kw: Any
) -> str:
    job = await build(c, tok, capp, case)
    r = await c.post(
        f"/v1/internal/cases/{case['id']}/claim/draft",
        json=crew_body(case, job, **kw),
        headers=tok("svc:crew"),
    )
    assert r.status_code == 200, r.text
    assert await status_of(c, tok, case["id"]) == "ready_for_review"
    return job


async def signed(
    c: httpx.AsyncClient, tok: Any, case: dict[str, Any], ack: list[str] | None = None
) -> httpx.Response:
    return await c.post(
        f"/v1/cases/{case['id']}/claim/signoff",
        headers=tok("officer1"),
        json={"decision": "approved", "comment": "verified", "acknowledged_warnings": ack or []},
    )


async def submitted(c: httpx.AsyncClient, tok: Any, capp: Any, **ready_kw: Any) -> dict[str, Any]:
    case = await ready_case(c, tok, **ready_kw)
    await to_review(c, tok, capp, case)
    assert (await signed(c, tok, case)).status_code == 200
    r = await c.post(f"/v1/cases/{case['id']}/claim/submit", headers=tok("officer1"), json={})
    assert r.status_code == 202, r.text
    return case


def force_due(settings: Any, case_id: str) -> None:
    sql(
        settings,
        "UPDATE outbox SET next_attempt_at = now() - interval '1 second' WHERE case_id=:c AND status='pending'",
        c=case_id,
    )


async def deliver(capp: Any, settings: Any, case_id: str) -> dict[str, int]:
    force_due(settings, case_id)
    return await capp.state.outbox.run_once(case_id=case_id)  # type: ignore[no-any-return]


# ------------------------------------------------------------------------------------------ builder
async def test_build_needs_docs_complete_and_roles(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    from tests.helpers import new_case

    c = await new_case(cclient, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    r = await cclient.post(f"/v1/cases/{c['id']}/claim/build", headers=tok("desk1"))
    assert (
        r.status_code == 409
        and r.json()["code"] == "invalid_transition"
        and "draft" in r.json()["detail"]
    )
    assert (
        await cclient.post(f"/v1/cases/{c['id']}/claim/build", headers=tok("hadmin"))
    ).status_code == 403
    assert (
        await cclient.post(f"/v1/cases/{c['id']}/claim/build", headers=tok("svc:n8n"))
    ).status_code == 403


async def test_build_happy_path_and_draft_read_model(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok)
    crew = capp.state.crew
    job = await build(cclient, tok, capp, case)
    sent = crew.jobs[-1]
    assert (
        sent["job_id"] == job
        and sent["case_id"] == case["id"]
        and len(sent["document_ids"]) == 3
        and sent["repair"] is None
    )
    assert (
        sent["callback"].endswith(f"/cases/{case['id']}/claim/draft")
        and sent["route"]["pipeline"] == "cashless"
    )
    assert await status_of(cclient, tok, case["id"]) == "building_claim"
    assert (
        await cclient.post(f"/v1/cases/{case['id']}/claim/build", headers=tok("desk1"))
    ).status_code == 409  # no double start
    assert (
        await cclient.get(f"/v1/cases/{case['id']}/claim", headers=tok("officer1"))
    ).status_code == 404  # nothing yet
    assert (
        await cclient.post(
            f"/v1/internal/cases/{case['id']}/claim/draft",
            json=crew_body(case, job),
            headers=tok("svc:n8n"),
        )
    ).status_code == 403  # only the crew
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/claim/draft",
        json=crew_body(case, job),
        headers=tok("svc:crew"),
    )
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["has_errors"] is False
    assert await status_of(cclient, tok, case["id"]) == "ready_for_review"
    d = (await cclient.get(f"/v1/cases/{case['id']}/claim", headers=tok("desk1"))).json()
    assert (
        d["version"] == 1
        and d["source"] == "agent"
        and d["etag"] == "draft-1"
        and d["has_errors"] is False
    )
    assert d["validation"]["errors"] == [] and d["validation"]["reconciliation"]["diff"] == "0.00"
    assert d["payload"]["totals"]["gross"] == "2000.00" and "patient.dob" in d["provenance"]
    assert (
        d["ready_to_submit"]["ok"] is False
        and "signoff_missing_or_stale" in d["ready_to_submit"]["reasons"]
    )
    assert (
        sql(
            settings,
            "SELECT count(*) FROM bill_line WHERE draft_id=(SELECT id FROM claim_draft WHERE case_id=:c)",
            c=case["id"],
        )[0][0]
        == 2
    )
    mi = sql(
        settings, "SELECT model_info->>'job_id' FROM claim_draft WHERE case_id=:c", c=case["id"]
    )[0][0]
    assert mi == job
    # duplicate result for the same job is idempotent
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/claim/draft",
        json=crew_body(case, job),
        headers=tok("svc:crew"),
    )
    assert r.json()["duplicate"] is True and r.json()["version"] == 1
    assert (
        await cclient.get(f"/v1/cases/{case['id']}/claim/versions", headers=tok("officer1"))
    ).json()["versions"][0]["version"] == 1
    events = [e.type for e in capp.state.hub.events if e.case_id == case["id"]]
    assert "claim.draft_ready" in events


async def test_bad_draft_shape_is_422_and_validators_decide(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    job = await build(cclient, tok, capp, case)
    body = crew_body(case, job)
    body["payload"]["surprise"] = 1
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/claim/draft", json=body, headers=tok("svc:crew")
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    body = crew_body(case, job)
    body["payload"]["totals"]["gross"] = 2000.0  # floats are never money
    assert (
        await cclient.post(
            f"/v1/internal/cases/{case['id']}/claim/draft", json=body, headers=tok("svc:crew")
        )
    ).status_code == 422


async def test_repair_loop_then_surface_errors(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    crew = capp.state.crew
    job = await build(cclient, tok, capp, case)
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/claim/draft",
        json=crew_body(case, job, v01_error=True),
        headers=tok("svc:crew"),
    )
    assert (
        r.json()["repair_requested"] == 1
        and await status_of(cclient, tok, case["id"]) == "building_claim"
    )
    rep = crew.jobs[-1]
    assert (
        rep["repair"]["round"] == 1
        and rep["repair"]["errors"][0]["code"] == "V01"
        and rep["repair"]["previous_draft_version"] == 1
    )
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/claim/draft",
        json=crew_body(case, job, 1),
        headers=tok("svc:crew"),
    )
    assert (
        r.json()["has_errors"] is False
        and await status_of(cclient, tok, case["id"]) == "ready_for_review"
    )
    d = (await cclient.get(f"/v1/cases/{case['id']}/claim", headers=tok("officer1"))).json()
    assert d["version"] == 2 and d["source"] == "repair"
    # an agent that never fixes it: after two repair rounds the errors are handed to the officer
    case2 = await ready_case(cclient, tok)
    job2 = await build(cclient, tok, capp, case2)
    for rnd in range(3):
        r = await cclient.post(
            f"/v1/internal/cases/{case2['id']}/claim/draft",
            json=crew_body(case2, job2, rnd, v01_error=True),
            headers=tok("svc:crew"),
        )
        assert r.status_code == 200
    assert r.json()["has_errors"] is True and "repair_requested" not in r.json()
    assert await status_of(cclient, tok, case2["id"]) == "ready_for_review"
    d2 = (await cclient.get(f"/v1/cases/{case2['id']}/claim", headers=tok("officer1"))).json()
    assert d2["has_errors"] and d2["validation"]["errors"][0]["code"] == "V01"
    r = await signed(cclient, tok, case2)
    assert (
        r.status_code == 422
        and r.json()["code"] == "draft_has_errors"
        and r.json()["errors"] == ["V01"]
    )


async def test_crew_down_returns_case_and_503(settings: Any, tok: Any) -> None:
    from app.main import create_checked_app
    from app.services.crew import RecordingCrew
    from app.services.n8n import RecordingN8n

    app = create_checked_app(
        settings.model_copy(update={"completeness_debounce_s": 0, "outbox_enabled": False}),
        crew=RecordingCrew(fail=True),
        n8n=RecordingN8n(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://h"
        ) as c:
            case = await ready_case(c, tok)
            r = await c.post(f"/v1/cases/{case['id']}/claim/build", headers=tok("desk1"))
            assert r.status_code == 503 and r.json()["code"] == "crew_unavailable"
            assert await status_of(c, tok, case["id"]) == "docs_complete"  # the loop restored it
            types = [
                x[0]
                for x in sql(
                    settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=case["id"]
                )
            ]
            assert "claim.build_failed" in types


async def test_build_timeout_job_returns_case(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    await build(cclient, tok, capp, case)
    await capp.state.redis.delete(f"cache:hosp:claimjob:{case['id']}")  # the job key expired
    r = await cclient.post("/v1/internal/jobs/claim-build-timeouts", headers=tok("svc:n8n"))
    assert r.status_code == 200 and r.json()["timed_out"] >= 1
    assert await status_of(cclient, tok, case["id"]) == "docs_complete"


# ---------------------------------------------------------------------------------------------- edit
async def test_edit_allowed_paths_versions_and_signoff_invalidation(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok)
    await to_review(cclient, tok, capp, case)
    h = tok("officer1")
    assert (await signed(cclient, tok, case)).status_code == 200
    url = f"/v1/cases/{case['id']}/claim"
    ops = [
        {"op": "replace", "path": "/bill_lines/0/qty", "value": "2"},
        {"op": "replace", "path": "/bill_lines/0/amount", "value": "2000.00"},
        {"op": "replace", "path": "/totals/gross", "value": "3000.00"},
        {"op": "replace", "path": "/totals/claimed", "value": "3000.00"},
    ]
    assert (await cclient.put(url, json=ops, headers=tok("desk1"))).status_code == 403
    assert (await cclient.put(url, json=ops, headers=h)).status_code == 412  # If-Match required
    assert (
        await cclient.put(url, json=ops, headers={**h, "If-Match": "draft-9"})
    ).status_code == 412
    bad = await cclient.put(url, json=ops, headers={**h, "If-Match": "draft-1"})
    assert bad.status_code == 200, bad.text
    out = bad.json()
    assert (
        out["version"] == 2
        and out["etag"] == "draft-2"
        and "bill_lines[0].qty" in out["fields_changed"]
    )
    assert (
        out["has_errors"] is True
    )  # V03/V08: qty 2 x 1000 = 2000 ok, but the bill document total no longer matches
    v = (await cclient.get(f"{url}/versions", headers=h)).json()["versions"]
    assert [x["source"] for x in v] == ["human_edit", "agent"] and v[0]["edit_summary"][
        "fields_changed"
    ]
    assert (
        sql(
            settings,
            "SELECT count(*) FROM signoff WHERE case_id=:c AND invalidated_at IS NOT NULL",
            c=case["id"],
        )[0][0]
        == 1
    )
    audit_payloads = " ".join(
        x[0]
        for x in sql(
            settings,
            "SELECT payload::text FROM audit_event WHERE case_id=:c AND event_type='claim.edited'",
            c=case["id"],
        )
    )
    assert (
        "3000.00" not in audit_payloads and "bill_lines[0].qty" in audit_payloads
    )  # names only, never values
    # forbidden and malformed paths
    for path in (
        "/patient/member_id",
        "/patient/dob",
        "/admission/admitted_on",
        "/claim_type",
        "/documents",
    ):
        r = await cclient.put(
            url,
            json=[{"op": "replace", "path": path, "value": "x"}],
            headers={**h, "If-Match": "draft-2"},
        )
        assert r.status_code == 409 and r.json()["code"] == "path_not_editable", path
    r = await cclient.put(
        url,
        json=[{"op": "replace", "path": "/bill_lines/99/qty", "value": "1"}],
        headers={**h, "If-Match": "draft-2"},
    )
    assert r.status_code == 422
    r = await cclient.put(
        url,
        json=[{"op": "teleport", "path": "/totals/gross", "value": "1"}],
        headers={**h, "If-Match": "draft-2"},
    )
    assert r.status_code == 422
    r = await cclient.put(
        url,
        json=[{"op": "replace", "path": "/totals/gross", "value": "12.5.5"}],
        headers={**h, "If-Match": "draft-2"},
    )
    assert r.status_code == 422  # re-validated through the strict model
    # remove + add lines renumber sequentially
    r = await cclient.put(
        url, json=[{"op": "remove", "path": "/bill_lines/0"}], headers={**h, "If-Match": "draft-2"}
    )
    assert r.status_code == 200
    nums = [
        ln["line_no"] for ln in (await cclient.get(url, headers=h)).json()["payload"]["bill_lines"]
    ]
    assert nums == [1]


async def test_edit_locked_after_submission(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    r = await cclient.put(
        f"/v1/cases/{case['id']}/claim",
        json=[{"op": "replace", "path": "/totals/gross", "value": "1.00"}],
        headers={**tok("officer1"), "If-Match": "draft-1"},
    )
    assert r.status_code == 409 and r.json()["code"] == "case_locked"


# ------------------------------------------------------------------------------------------ sign-off
async def test_signoff_rules(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok, pharmacy_total="2000.00")
    await to_review(
        cclient, tok, capp, case, duplicate=True
    )  # V09 duplicate-line warning needs acknowledgement
    h = tok("officer1")
    assert (
        await cclient.post(
            f"/v1/cases/{case['id']}/claim/signoff",
            headers=tok("desk1"),
            json={"decision": "approved"},
        )
    ).status_code == 403
    r = await signed(cclient, tok, case)
    assert (
        r.status_code == 422
        and r.json()["code"] == "warnings_not_acknowledged"
        and r.json()["missing"] == ["V09"]
    )
    assert (
        await cclient.post(
            f"/v1/cases/{case['id']}/claim/signoff", headers=h, json={"decision": "maybe"}
        )
    ).status_code == 422
    r = await signed(cclient, tok, case, ["V09"])
    assert r.status_code == 200 and r.json()["draft_version"] == 1
    assert (await signed(cclient, tok, case, ["V09"])).status_code == 409  # already signed
    d = (await cclient.get(f"/v1/cases/{case['id']}/claim", headers=h)).json()
    assert d["signoff"]["acknowledged_warnings"] == ["V09"] and d["ready_to_submit"]["ok"] is True
    types = [
        x[0]
        for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=case["id"])
    ]
    assert "human.signoff" in types


async def test_signoff_returned_sends_case_back(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    await to_review(cclient, tok, capp, case)
    r = await cclient.post(
        f"/v1/cases/{case['id']}/claim/signoff",
        headers=tok("officer1"),
        json={"decision": "returned", "comment": "bill page 2 unreadable"},
    )
    assert r.status_code == 200 and r.json()["decision"] == "returned"
    assert await status_of(cclient, tok, case["id"]) in (
        "docs_pending",
        "docs_complete",
    )  # completeness loop may re-complete it


async def test_route_warning_must_be_acknowledged_before_signoff(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(
        cclient, tok, preauth_ref=None
    )  # cashless proposed, router says reimbursement
    await to_review(cclient, tok, capp, case)
    r = await signed(cclient, tok, case)
    assert r.status_code == 422 and r.json()["missing"] == ["claim_type_disagrees_with_selection"]
    assert (
        await cclient.post(f"/v1/cases/{case['id']}/route/ack", json={}, headers=tok("officer1"))
    ).status_code == 200
    assert (await signed(cclient, tok, case)).status_code == 200


# ------------------------------------------------------------------------------------------- submit
async def test_submit_requires_signoff_and_detects_stale(
    cclient: httpx.AsyncClient, tok: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    await to_review(cclient, tok, capp, case)
    url = f"/v1/cases/{case['id']}/claim/submit"
    h = tok("officer1")
    r = await cclient.post(url, headers=h, json={})
    assert r.status_code == 409 and r.json()["code"] == "signoff_required"
    assert (await cclient.post(url, headers=tok("desk1"), json={})).status_code == 403
    assert (await signed(cclient, tok, case)).status_code == 200
    r = await cclient.put(
        f"/v1/cases/{case['id']}/claim",
        json=[{"op": "replace", "path": "/admission/treating_doctor", "value": "Dr. New"}],
        headers={**h, "If-Match": "draft-1"},
    )
    assert r.status_code == 200
    r = await cclient.post(url, headers=h, json={})
    assert (
        r.status_code == 409 and r.json()["code"] == "signoff_required"
    )  # the sign-off was invalidated by the edit
    assert (await signed(cclient, tok, case)).status_code == 200  # sign the new version
    assert (await cclient.post(url, headers=h, json={})).status_code == 202


async def test_submit_effects_outbox_body_and_idempotency(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    from claim_contract.models import ClaimSubmission

    case = await submitted(cclient, tok, capp)
    cid = case["id"]
    assert await status_of(cclient, tok, cid) == "submitted"
    row = sql(
        settings,
        "SELECT kind, method, path, status, sequence, idempotency_key::text, body_sha256, body FROM outbox WHERE case_id=:c",
        c=cid,
    )
    assert len(row) == 1
    kind, method, path, status, seq, idem, sha, body = row[0]
    assert (kind, method, path, status, seq) == (
        "claim.submit",
        "POST",
        "/v1/hospital-api/claims",
        "pending",
        1,
    )
    sub = ClaimSubmission.model_validate(body)  # the exact contract model the insurer will validate
    assert sub.claim_ref == case["claim_ref"] and sub.contract_version == "1.1" and sub.journey_id
    assert (
        str(sub.totals.claimed.amount) == "2000.00"
        and len(sub.documents) == 3
        and len(sub.bill_lines) == 2
    )
    assert (
        "id_proof" not in json.dumps(body)
        or body["patient"]["id_proof_hash"] is None
        or len(body["patient"]["id_proof_hash"]) == 64
    )
    assert all(
        d["download_url"].startswith("http") and len(d["sha256"]) == 64 for d in body["documents"]
    )
    assert uuid.UUID(idem) and len(sha) == 64
    async with httpx.AsyncClient() as ext:
        got = await ext.get(body["documents"][0]["download_url"])
        assert got.status_code == 200 and len(got.content) == body["documents"][0]["size_bytes"]
    r = await cclient.post(f"/v1/cases/{cid}/claim/submit", headers=tok("officer1"), json={})
    assert r.status_code == 409 and r.json()["code"] == "already_submitted"
    assert sql(settings, "SELECT claimed_amount FROM claim_case WHERE id=:i", i=cid)[0][0] == 2000
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert "claim.submitted" in types
    sub_status = (await cclient.get(f"/v1/cases/{cid}/submission", headers=tok("desk1"))).json()
    assert (
        sub_status["status"] == "pending"
        and sub_status["attempts"] == 0
        and sub_status["ready_to_submit"]["ok"] is False
    )


async def test_concurrent_submit_exactly_one_wins(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok)
    await to_review(cclient, tok, capp, case)
    await signed(cclient, tok, case)
    url = f"/v1/cases/{case['id']}/claim/submit"
    rs = await asyncio.gather(
        *[cclient.post(url, headers=tok("officer1"), json={}) for _ in range(4)]
    )
    assert sorted(r.status_code for r in rs).count(202) == 1
    assert {r.json()["code"] for r in rs if r.status_code != 202} == {"already_submitted"}
    assert sql(settings, "SELECT count(*) FROM outbox WHERE case_id=:c", c=case["id"])[0][0] == 1


async def test_late_filing_requires_reason(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(
        cclient,
        tok,
        claim_type="reimbursement",
        preauth_ref=None,
        admitted_on="2026-07-20",
        discharged_on="2026-08-01",
    )
    await cclient.post(
        f"/v1/cases/{case['id']}/route/ack", json={}, headers=tok("officer1")
    )  # late_filing needs acknowledgement
    await to_review(cclient, tok, capp, case)
    r = await cclient.post(
        f"/v1/cases/{case['id']}/claim/signoff",
        headers=tok("officer1"),
        json={"decision": "approved"},
    )
    assert r.status_code == 200, r.text
    url = f"/v1/cases/{case['id']}/claim/submit"
    r = await cclient.post(url, headers=tok("officer1"), json={})
    assert r.status_code == 422 and r.json()["code"] == "filing_deadline_passed"
    r = await cclient.post(
        url,
        headers=tok("officer1"),
        json={"late_filing_reason": "Patient was abroad until September"},
    )
    assert r.status_code == 202
    body = sql(
        settings, "SELECT body->>'hospital_notes' FROM outbox WHERE case_id=:c", c=case["id"]
    )[0][0]
    assert body == "Late filing: Patient was abroad until September"


# ---------------------------------------------------------------------------------- outbox delivery
async def test_delivery_acknowledges_case_and_records_insurer_claim_no(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid = case["id"]
    stats = await deliver(capp, settings, cid)
    assert stats["claimed"] >= 1 and stats["errors"] == 0
    sim = capp.state.sim
    assert case["claim_ref"] in sim.claims
    assert await status_of(cclient, tok, cid) == "acknowledged"
    s = (await cclient.get(f"/v1/cases/{cid}/submission", headers=tok("officer1"))).json()
    assert (
        s["status"] == "sent"
        and s["attempts"] == 1
        and s["insurer_claim_no"].startswith("IC-")
        and s["acknowledged_at"]
    )
    row = sql(
        settings,
        "SELECT status, response_status, sent_at IS NOT NULL FROM outbox WHERE case_id=:c",
        c=cid,
    )[0]
    assert row == ("sent", 202, True)
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert {"ack.received", "submission.sent"} <= set(types)
    hdr = sim.received[-1]["headers"]
    assert (
        hdr["x-key-id"] == "hosp-001"
        and hdr["x-idempotency-key"]
        and hdr["x-journey-id"]
        and hdr["x-signature"]
    )
    assert (await deliver(capp, settings, cid))["claimed"] >= 0 and len(
        [r for r in sim.received if r["body"]["claim_ref"] == case["claim_ref"]]
    ) == 1


async def test_transient_failures_retry_with_backoff_then_succeed(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim = case["id"], capp.state.sim
    sim.fail_next = 3
    for expected in (1, 2, 3):
        await deliver(capp, settings, cid)
        row = sql(
            settings,
            "SELECT status, attempts, next_attempt_at > now(), last_error FROM outbox WHERE case_id=:c",
            c=cid,
        )[0]
        assert (
            row[0] == "pending" and row[1] == expected and row[2] is True
        )  # backoff scheduled in the future
        assert "503" in row[3]
    res = await deliver(capp, settings, cid)
    row = sql(settings, "SELECT status, attempts FROM outbox WHERE case_id=:c", c=cid)[0]
    assert row == ("sent", 4), (
        res,
        sql(
            settings,
            "SELECT status, attempts, last_error, next_attempt_at, now() FROM outbox WHERE case_id=:c",
            c=cid,
        ),
    )
    assert await status_of(cclient, tok, cid) == "acknowledged"
    assert len(sim.claims) >= 1


async def test_terminal_4xx_fails_then_reopen_and_resubmit(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim, h = case["id"], capp.state.sim, tok("officer1")
    sim.reject_code = "totals_mismatch"
    await deliver(capp, settings, cid)
    row = sql(
        settings, "SELECT status, attempts, response_status FROM outbox WHERE case_id=:c", c=cid
    )[0]
    assert row == ("failed", 1, 422)
    st = (await cclient.get(f"/v1/cases/{cid}/submission", headers=h)).json()
    assert st["status"] == "failed" and st["last_error"]["problem"]["code"] == "totals_mismatch"
    assert await status_of(cclient, tok, cid) == "submitted"  # not acknowledged
    n_before = len(sim.claims)
    assert (
        await cclient.post(f"/v1/cases/{cid}/submission/retry", headers=h)
    ).status_code == 409  # failed is not dead
    r = await cclient.post(f"/v1/cases/{cid}/submission/reopen", headers=h)
    assert r.status_code == 200 and await status_of(cclient, tok, cid) == "ready_for_review"
    r = await cclient.put(
        f"/v1/cases/{cid}/claim",
        json=[{"op": "replace", "path": "/admission/treating_doctor", "value": "Dr. Fixed"}],
        headers={**h, "If-Match": "draft-1"},
    )
    assert r.status_code == 200
    assert (await signed(cclient, tok, case)).status_code == 200
    r = await cclient.post(f"/v1/cases/{cid}/claim/submit", headers=h, json={})
    assert r.status_code == 202
    keys = sql(
        settings,
        "SELECT idempotency_key::text, status FROM outbox WHERE case_id=:c ORDER BY sequence",
        c=cid,
    )
    assert len({k[0] for k in keys}) == 2 and keys[0][1] == "failed"
    await deliver(capp, settings, cid)
    assert await status_of(cclient, tok, cid) == "acknowledged" and len(sim.claims) == n_before + 1


async def test_dead_letter_after_eight_attempts_and_retry_reuses_key(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim, h = case["id"], capp.state.sim, tok("officer1")
    key = sql(settings, "SELECT idempotency_key::text FROM outbox WHERE case_id=:c", c=cid)[0][0]
    sim.fail_next = 99
    for _ in range(8):
        await deliver(capp, settings, cid)
    row = sql(settings, "SELECT status, attempts FROM outbox WHERE case_id=:c", c=cid)[0]
    assert row == ("dead", 8)
    assert {"submission.dead"} <= {
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    }
    assert any(e.type == "submission.dead" for e in capp.state.hub.events if e.case_id == cid)
    dead = (await cclient.get("/v1/admin/outbox?status=dead", headers=tok("hadmin"))).json()[
        "items"
    ]
    assert case["claim_ref"] in [d["claim_ref"] for d in dead]
    assert (await cclient.get("/v1/admin/outbox?status=dead", headers=h)).status_code == 403
    assert (
        await status_of(cclient, tok, cid) == "submitted"
    )  # stays submitted; the UI shows a retry banner
    sim.fail_next = 0
    r = await cclient.post(f"/v1/cases/{cid}/submission/retry", headers=h)
    assert r.status_code == 200
    after = sql(
        settings,
        "SELECT status, attempts, idempotency_key::text FROM outbox WHERE case_id=:c",
        c=cid,
    )[0]
    assert (
        after[0] == "pending" and after[1] == 0 and after[2] == key
    )  # same key, so a late success is never duplicated
    await deliver(capp, settings, cid)
    assert await status_of(cclient, tok, cid) == "acknowledged"


async def test_crash_after_send_before_db_update_is_exactly_once(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim, worker = case["id"], capp.state.sim, capp.state.outbox
    original = worker._success  # noqa: SLF001

    async def crash(row: Any, status: int, body: Any) -> None:
        raise RuntimeError("worker killed between HTTP send and DB update")

    worker._success = crash  # type: ignore[method-assign]  # noqa: SLF001
    try:
        await deliver(capp, settings, cid)
    finally:
        worker._success = original  # type: ignore[method-assign]  # noqa: SLF001
    assert (
        sql(settings, "SELECT status FROM outbox WHERE case_id=:c", c=cid)[0][0] == "sending"
    )  # stuck, never recorded
    assert case["claim_ref"] in sim.claims
    # the janitor frees it after 60 s; simulate the passage of time and restart the worker
    sql(
        settings,
        "UPDATE outbox SET created_at = now() - interval '5 minutes', next_attempt_at = now() - interval '5 minutes', "
        "sent_at = NULL WHERE case_id=:c",
        c=cid,
    )
    await capp.state.outbox.run_once(case_id=cid)
    assert sql(settings, "SELECT status FROM outbox WHERE case_id=:c", c=cid)[0][0] == "sent"
    mine = [r for r in sim.received if r["body"].get("claim_ref") == case["claim_ref"]]
    # the resend carried the same idempotency key: the insurer replayed its stored answer instead of executing again
    assert len(mine) == 1 and list(sim.claims).count(case["claim_ref"]) == 1


async def test_per_claim_ordering_documents_never_overtake_submit(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim = case["id"], capp.state.sim
    sim.fail_next = 1  # the head (claim.submit) fails once
    sql(
        settings,
        "INSERT INTO outbox (id, case_id, kind, method, path, body, body_sha256, idempotency_key, sequence, status) VALUES "
        "(uuid_generate_v7(), :c, 'claim.documents', 'POST', :p, CAST(:b AS jsonb), repeat('0',64), uuid_generate_v7(), 2, 'pending')",
        c=cid,
        p=f"/v1/hospital-api/claims/{case['claim_ref']}/documents",
        b=json.dumps({"reason": "voluntary", "documents": []}),
    )
    await deliver(capp, settings, cid)  # submit fails -> documents row must not be sent
    rows = sql(
        settings, "SELECT kind, status FROM outbox WHERE case_id=:c ORDER BY sequence", c=cid
    )
    assert rows == [("claim.submit", "pending"), ("claim.documents", "pending")]
    assert not sim.supplements
    await deliver(capp, settings, cid)  # submit now succeeds
    await deliver(capp, settings, cid)  # then the documents row is delivered
    rows = sql(
        settings, "SELECT kind, status FROM outbox WHERE case_id=:c ORDER BY sequence", c=cid
    )
    assert [r[1] for r in rows][0] == "sent"


async def test_clock_skew_requeues_without_burning_attempts(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any, monkeypatch: Any
) -> None:
    from datetime import UTC, datetime, timedelta

    from claim_contract import signing

    case = await submitted(cclient, tok, capp)
    cid = case["id"]
    real = signing.now_ts
    monkeypatch.setattr(
        signing,
        "now_ts",
        lambda: (datetime.now(UTC) + timedelta(minutes=10)).strftime(signing.TS_FORMAT),
    )
    await deliver(capp, settings, cid)
    row = sql(settings, "SELECT status, attempts FROM outbox WHERE case_id=:c", c=cid)[0]
    assert row == ("pending", 0)  # stale_request: re-signed immediately, no attempt penalty
    monkeypatch.setattr(signing, "now_ts", real)
    await deliver(capp, settings, cid)
    assert sql(settings, "SELECT status FROM outbox WHERE case_id=:c", c=cid)[0][0] == "sent"


async def test_withdraw_flows_through_outbox(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)
    cid, sim, h = case["id"], capp.state.sim, tok("officer1")
    await deliver(capp, settings, cid)
    assert (
        await cclient.post(
            f"/v1/cases/{cid}/claim/withdraw", headers=tok("desk1"), json={"reason": "duplicate"}
        )
    ).status_code == 403
    assert (
        await cclient.post(
            f"/v1/cases/{cid}/claim/withdraw", headers=h, json={"reason": "nonsense"}
        )
    ).status_code == 422
    r = await cclient.post(
        f"/v1/cases/{cid}/claim/withdraw",
        headers=h,
        json={"reason": "patient_requested", "note": "changed mind"},
    )
    assert r.status_code == 200
    assert (
        await cclient.post(
            f"/v1/cases/{cid}/claim/withdraw", headers=h, json={"reason": "duplicate"}
        )
    ).status_code == 409
    await deliver(capp, settings, cid)
    assert case["claim_ref"] in sim.withdrawn and await status_of(cclient, tok, cid) == "closed"


# -------------------------------------------------------------------------------------- callbacks
async def acked(cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any) -> dict[str, Any]:
    case = await submitted(cclient, tok, capp)
    await deliver(capp, settings, case["id"])
    got = await status_of(cclient, tok, case["id"])
    assert got == "acknowledged", sql(
        settings,
        "SELECT kind, status, attempts, last_error, response_status FROM outbox WHERE case_id=:c",
        c=case["id"],
    )
    return case


def st(status: str, vis: str | None = None, note: str | None = None) -> dict[str, Any]:
    from claim_contract.enums import InsurerCaseStatus
    from claim_contract.transitions import HOSPITAL_VISIBLE

    return {
        "status": status,
        "hospital_visible_status": vis or HOSPITAL_VISIBLE[InsurerCaseStatus(status)].value,
        "occurred_at": "2026-10-06T10:00:00Z",
        "note": note,
        "open_query_ids": [],
    }


async def test_status_callbacks_map_to_hospital_status(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    r = await sim.push(ref, "status", st("verifying"))
    assert r.status_code == 204 and await status_of(cclient, tok, cid) == "acknowledged"
    r = await sim.push(ref, "status", st("needs_info"))
    assert r.status_code == 204 and await status_of(cclient, tok, cid) == "under_query"
    assert (
        sql(settings, "SELECT insurer_status FROM claim_case WHERE id=:i", i=cid)[0][0]
        == "needs_info"
    )
    r = await sim.push(ref, "status", st("verifying"))
    assert await status_of(cclient, tok, cid) == "acknowledged"
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert types.count("callback.status") == 3
    assert any(
        e.type == "case.status_changed" and e.data.get("to") == "under_query"
        for e in capp.state.hub.events
        if e.case_id == cid
    )


async def test_callback_authentication_and_dedupe_matrix(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    assert (
        await sim.push(ref, "status", st("verifying"), tamper=True)
    ).status_code == 401  # bad signature
    r = await sim.push(ref, "status", st("verifying"), ts="2020-01-01T00:00:00Z")
    assert r.status_code == 401 and r.json()["code"] == "stale_request"
    idem = str(uuid.uuid4())
    seq = sim.next_seq(ref)
    first = await sim.push(ref, "status", st("verifying"), sequence=seq, idem=idem)
    again = await sim.push(ref, "status", st("verifying"), sequence=seq, idem=idem)
    assert (
        first.status_code == again.status_code == 204
        and again.headers.get("idempotent-replay") == "true"
    )
    assert (
        sql(
            settings,
            "SELECT count(*) FROM inbound_callback WHERE claim_ref=:r AND sequence=:s",
            r=ref,
            s=seq,
        )[0][0]
        == 1
    )
    r = await sim.push(
        ref, "status", st("verifying"), sequence=seq, idem=str(uuid.uuid4())
    )  # new key, same body
    assert r.status_code == 204  # recognised duplicate by sequence
    r = await sim.push(
        ref, "status", st("needs_info"), sequence=seq, idem=str(uuid.uuid4())
    )  # same sequence, different body
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"
    r = await sim.push("HC-2026-999999", "status", st("verifying"), sequence=1) if False else None
    sim.claims["HC-2026-999999"] = {
        "ack": sim.claims[ref]["ack"],
        "seq": 1,
        "status": "x",
        "queries": {},
        "sub": None,
    }
    r = await sim.push("HC-2026-999999", "status", st("verifying"))
    assert r.status_code == 404 and r.json()["code"] == "unknown_claim"
    # invalid body and unknown insurer status
    r = await sim.push(ref, "status", {**st("verifying"), "status": "teleported"})
    assert r.status_code == 422
    assert (
        await cclient.post(
            "/v1/insurer-callbacks/status", json={}, headers={"X-Contract-Version": "1.1"}
        )
    ).status_code == 401  # unsigned
    assert await status_of(cclient, tok, cid) == "acknowledged"


async def test_older_status_is_ignored_and_backward_transitions_recorded(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    s5 = sim.next_seq(ref) + 2  # a gap: sequences 2.. skipped, 5 arrives first
    await sim.push(ref, "status", st("needs_info"), sequence=s5)
    assert await status_of(cclient, tok, cid) == "under_query"
    await sim.push(
        ref, "status", st("verifying"), sequence=s5 - 1
    )  # older and stale: must not undo needs_info
    assert await status_of(cclient, tok, cid) == "under_query"
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert "callback.sequence_gap" in types
    # a settled case cannot go back to a decision state
    await sim.push(ref, "decisions", {"decision": decision_body("approve")})
    await sim.push(ref, "settlements", {"settlement": settlement_body("UTR-0001", "2000.00")})
    assert await status_of(cclient, tok, cid) == "settled"
    await sim.push(ref, "status", st("needs_info"))
    assert await status_of(cclient, tok, cid) == "settled"
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert "callback.ignored_invalid_transition" in types


def decision_body(
    outcome: str, approved: str = "2000.00", deductions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "approved_amount": {"amount": approved},
        "deductions": deductions or [],
        "reason_codes": ["ROOM_CAP"] if deductions else [],
        "reviewer_ids": ["rev-1"],
        "calc_trace_id": str(uuid.uuid4()),
        "policy_version": 6,
        "decided_at": "2026-10-06T11:00:00Z",
    }


def settlement_body(utr: str, amount: str) -> dict[str, Any]:
    return {
        "settlement_id": str(uuid.uuid4()),
        "amount": {"amount": amount},
        "utr": utr,
        "paid_on": "2026-10-07",
        "mode": "NEFT",
        "tds": {"amount": "0.00"},
    }


async def test_decision_and_settlement_flow(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    ded = [
        {
            "line_ref": "RM-1",
            "rule_id": "room_rent_cap",
            "amount": {"amount": "200.00"},
            "explanation": "Room rent capped.",
        }
    ]
    r = await sim.push(ref, "decisions", {"decision": decision_body("partial", "1800.00", ded)})
    assert r.status_code == 204 and await status_of(cclient, tok, cid) == "partially_approved"
    row = sql(
        settings,
        "SELECT approved_amount, short_pay_amount, decision->>'outcome' FROM claim_case WHERE id=:i",
        i=cid,
    )[0]
    assert (row[0], row[1], row[2]) == (1800, 200, "partial")
    r = await sim.push(ref, "settlements", {"settlement": settlement_body("UTR-777", "1800.00")})
    assert r.status_code == 204 and await status_of(cclient, tok, cid) == "settled"
    assert sql(settings, "SELECT settled_amount FROM claim_case WHERE id=:i", i=cid)[0][0] == 1800
    sid = str(uuid.uuid4())
    body = settlement_body("UTR-777", "1800.00")
    body["settlement_id"] = sid
    await sim.push(ref, "settlements", {"settlement": body})  # same UTR again: ignored
    assert sql(settings, "SELECT count(*) FROM settlement WHERE case_id=:c", c=cid)[0][0] == 1
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert {"callback.decision", "callback.settlement"} <= set(types)
    utr_audit = " ".join(
        x[0] for x in sql(settings, "SELECT payload::text FROM audit_event WHERE case_id=:c", c=cid)
    )
    assert "UTR-777" not in utr_audit  # only a hash of the UTR is audited


async def test_reject_and_exceeds_claim_anomaly(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await acked(cclient, tok, capp, settings)
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    r = await sim.push(
        ref, "decisions", {"decision": decision_body("approve", "2500.00")}
    )  # more than claimed (2000)
    assert r.status_code == 204 and await status_of(cclient, tok, cid) == "approved"
    types = [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert "decision.exceeds_claim" in types  # recorded, not rejected
    case2 = await acked(cclient, tok, capp, settings)
    r = await sim.push(
        case2["claim_ref"], "decisions", {"decision": decision_body("reject", "0.00")}
    )
    assert r.status_code == 204 and await status_of(cclient, tok, case2["id"]) == "rejected"
    bad = decision_body("reject", "10.00")  # reject with an amount violates the contract model
    r = await sim.push(case2["claim_ref"], "decisions", {"decision": bad})
    assert r.status_code == 422


async def test_callback_before_ack_upserts_claim_no_and_advances(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await submitted(cclient, tok, capp)  # not delivered yet: still `submitted`
    cid, ref, sim = case["id"], case["claim_ref"], capp.state.sim
    from claim_contract import models as m

    sim.claims[ref] = {
        "sub": None,
        "ack": m.Acknowledgement(
            claim_ref=ref,
            insurer_claim_no="IC-2026-000777",
            status="received",
            received_at="2026-10-06T10:00:00Z",
            sequence=1,
            document_ingest={"queued": 3, "failed": 0},
        ),  # type: ignore[arg-type]
        "status": "received",
        "seq": 1,
        "queries": {},
    }
    r = await sim.push(ref, "status", st("needs_info"))
    assert r.status_code == 204
    assert (
        await status_of(cclient, tok, cid) == "under_query"
    )  # submitted -> acknowledged -> under_query
    assert (
        sql(settings, "SELECT insurer_claim_no FROM claim_case WHERE id=:i", i=cid)[0][0]
        == "IC-2026-000777"
    )


async def test_refresh_url_callback(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    from claim_contract import signing

    case = await acked(cclient, tok, capp, settings)
    ref = case["claim_ref"]
    doc = case["docs"]["final_bill"]

    async def call(path: str, body: dict[str, Any]) -> httpx.Response:
        raw = json.dumps(body).encode()
        ts, idem = signing.now_ts(), str(uuid.uuid4())
        sig = signing.sign(
            settings.insurer_to_hospital_secret.encode(), "POST", path, ts, idem, raw
        )
        return await cclient.post(
            path,
            content=raw,
            headers={
                "X-Contract-Version": "1.1",
                "X-Key-Id": "ins-001",
                "X-Timestamp": ts,
                "X-Idempotency-Key": idem,
                "X-Signature": sig,
                "Content-Type": "application/json",
            },
        )

    for k in [k async for k in capp.state.redis.scan_iter("rl:hosp:refresh:*")]:
        await capp.state.redis.delete(k)
    r = await call(
        f"/v1/insurer-callbacks/documents/{doc}/refresh-url",
        {"claim_ref": ref, "reason": "url_expired"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["doc_id"] == doc and len(j["sha256"]) == 64
    async with httpx.AsyncClient() as ext:
        assert (await ext.get(j["download_url"])).status_code == 200
    other = await acked(cclient, tok, capp, settings)  # another claim: the document is not theirs
    r = await call(
        f"/v1/insurer-callbacks/documents/{doc}/refresh-url",
        {"claim_ref": other["claim_ref"], "reason": "url_expired"},
    )
    assert r.status_code == 403 and r.json()["code"] == "not_your_claim"
    r = await call(
        f"/v1/insurer-callbacks/documents/{uuid.uuid4()}/refresh-url",
        {"claim_ref": ref, "reason": "url_expired"},
    )
    assert r.status_code == 404 and r.json()["code"] == "unknown_document"
    r = await call(
        f"/v1/insurer-callbacks/documents/{doc}/refresh-url",
        {"claim_ref": "HC-2026-000000", "reason": "url_expired"},
    )
    assert r.status_code == 404 and r.json()["code"] == "unknown_claim"
    codes = [
        (
            await call(
                f"/v1/insurer-callbacks/documents/{doc}/refresh-url",
                {"claim_ref": ref, "reason": "url_expired"},
            )
        ).status_code
        for _ in range(11)
    ]
    assert 429 in codes


async def test_end_to_end_claim_lifecycle_and_audit_chain(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok)
    cid, ref, sim, h = case["id"], case["claim_ref"], capp.state.sim, tok("officer1")
    job = await build(cclient, tok, capp, case)  # build -> repair -> ready
    await cclient.post(
        f"/v1/internal/cases/{cid}/claim/draft",
        json=crew_body(case, job, v01_error=True),
        headers=tok("svc:crew"),
    )
    await cclient.post(
        f"/v1/internal/cases/{cid}/claim/draft",
        json=crew_body(case, job, 1),
        headers=tok("svc:crew"),
    )
    r = await cclient.put(
        f"/v1/cases/{cid}/claim",
        json=[{"op": "replace", "path": "/admission/treating_doctor", "value": "Dr. Final"}],
        headers={**h, "If-Match": "draft-2"},
    )
    assert r.status_code == 200
    assert (await signed(cclient, tok, case)).status_code == 200
    assert (
        await cclient.post(f"/v1/cases/{cid}/claim/submit", headers=h, json={})
    ).status_code == 202
    sim.fail_next = 1
    await deliver(capp, settings, cid)  # 503 once
    await deliver(capp, settings, cid)  # then 202
    assert await status_of(cclient, tok, cid) == "acknowledged"
    await sim.push(ref, "status", st("verifying"))
    await sim.push(ref, "decisions", {"decision": decision_body("partial", "1800.00")})
    await sim.push(ref, "settlements", {"settlement": settlement_body("UTR-E2E", "1800.00")})
    assert await status_of(cclient, tok, cid) == "settled"
    async with capp.state.sessionmaker() as s:
        res = await audit_svc.verify(s, cid)
    assert res.ok and res.count >= 15, res
    tl = (await cclient.get(f"/v1/cases/{cid}/timeline?size=200", headers=h)).json()["events"]
    statuses = [e["to"] for e in tl if e["kind"] == "status"][::-1]
    assert statuses[:8] == [
        "draft",
        "docs_pending",
        "docs_complete",
        "building_claim",
        "ready_for_review",
        "submitted",
        "acknowledged",
        "partially_approved",
    ]
    assert statuses[-1] == "settled"
