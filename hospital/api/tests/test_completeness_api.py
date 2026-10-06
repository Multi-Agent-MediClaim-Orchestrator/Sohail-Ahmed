"""Completeness engine end to end: persistence, doc requests, status driving, waivers, scheduler, config admin."""

import asyncio
import uuid
from copy import deepcopy
from typing import Any

import httpx
import pytest
from app.completeness.service import CompletenessScheduler
from seed.config_payloads import DOC_REQUIREMENTS
from sqlalchemy import create_engine, text
from tests.helpers import files, new_case, pdf_bytes
from tests.test_documents import sql, sync_engine

pytestmark = pytest.mark.integration
PDF = "application/pdf"


def U() -> str:
    return "UH-" + uuid.uuid4().hex[:8]


async def add_doc(
    c: httpx.AsyncClient,
    tok: Any,
    case_id: str,
    doc_type: str,
    *,
    stamp: bool | None = True,
    conf: float = 0.92,
    agree_total: str | None = None,
    typed: dict[str, Any] | None = None,
    flags: list[str] | None = None,
    filename: str = "d.pdf",
) -> str:
    """Upload a document and drive it through the pipeline callbacks until it is parsed."""
    r = await c.post(
        f"/v1/cases/{case_id}/documents",
        headers=tok("desk1"),
        data={"doc_type_hint": doc_type},
        files=files((filename, pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
    )
    assert r.status_code == 202, r.text
    doc = r.json()["documents"][0]["id"]
    n8n, crew = tok("svc:n8n"), tok("svc:crew")
    q: dict[str, Any] = {"quality_score": 0.9, "flags": flags or [], "has_required_stamp": stamp}
    assert (
        await c.post(f"/v1/internal/documents/{doc}/quality", json=q, headers=n8n)
    ).status_code == 200
    body = (
        typed
        if typed is not None
        else {
            "patient_name": "Ravi Kumar",
            "date": "2026-09-29",
            "total": "1000.00",
            "lines": [{"amount": "1000.00"}],
            "medicines": ["Paracetamol"],
        }
    )
    p1 = {"pass_no": 1, "engine": "mineru-pipeline", "confidence": conf, "typed_json": body}
    assert (
        await c.post(f"/v1/internal/documents/{doc}/parse", json=p1, headers=n8n)
    ).status_code == 200
    p2 = {"pass_no": 2, "engine": "crew-llm", "confidence": conf, "typed_json": body}
    assert (
        await c.post(f"/v1/internal/documents/{doc}/parse", json=p2, headers=crew)
    ).status_code == 200
    return doc


async def comp(
    c: httpx.AsyncClient, tok: Any, case_id: str, who: str = "officer1"
) -> dict[str, Any]:
    r = await c.get(f"/v1/cases/{case_id}/completeness", headers=tok(who))
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def by_rule(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["rule_id"]: i for i in out["items"]}


async def test_empty_case_has_blockers_requests_and_reminders(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    out = await comp(rclient, tok, case["id"])  # first read triggers a run
    assert out["run_no"] == 1 and out["complete"] is False and out["config_version"] >= 1
    rules = by_rule(out)
    assert {k: v["status"] for k, v in rules.items()} == {
        "R-RX-01": "missing",
        "R-PH-01": "missing",
        "R-BILL-01": "missing",
    }
    assert out["summary"]["blockers"] == 3
    reqs = (await rclient.get(f"/v1/cases/{case['id']}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    assert sorted(r["rule_id"] for r in reqs) == ["R-BILL-01", "R-PH-01", "R-RX-01"]
    assert all(
        r["reason_code"] == "not_uploaded" and r["due_by"] and "required" in r["message"]
        for r in reqs
    )
    n = sql(
        settings,
        "SELECT count(*) FROM reminder WHERE case_id=:c AND kind='doc_request' AND status='scheduled'",
        c=case["id"],
    )
    assert n[0][0] == 9  # 3 requests x offsets [24, 48, 72]
    assert (await rclient.get(f"/v1/cases/{case['id']}", headers=tok("officer1"))).json()[
        "status"
    ] == "draft"  # no docs yet


async def test_full_flow_to_docs_complete(
    rclient: httpx.AsyncClient, tok: Any, rapp: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await add_doc(rclient, tok, cid, "prescription", conf=0.92)
    out = await comp(rclient, tok, cid)
    assert (
        by_rule(out)["R-RX-01"]["status"] == "present_ok"
        and by_rule(out)["R-PH-01"]["status"] == "missing"
    )
    assert (await rclient.get(f"/v1/cases/{cid}", headers=tok("officer1"))).json()[
        "status"
    ] == "docs_pending"
    await add_doc(rclient, tok, cid, "pharmacy_bill")
    await add_doc(rclient, tok, cid, "final_bill")
    out = await comp(rclient, tok, cid)
    assert out["complete"] is True and out["summary"]["blockers"] == 0
    assert (await rclient.get(f"/v1/cases/{cid}", headers=tok("officer1"))).json()[
        "status"
    ] == "docs_complete"
    reqs = (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    assert reqs == []  # all requests fulfilled
    rows = sql(
        settings,
        "SELECT status::text, fulfilled_by_doc IS NOT NULL FROM doc_request WHERE case_id=:c",
        c=cid,
    )
    assert {r[0] for r in rows} == {"fulfilled"} and all(r[1] for r in rows)
    sched = sql(
        settings, "SELECT count(*) FROM reminder WHERE case_id=:c AND status='scheduled'", c=cid
    )[0][0]
    assert sched == 0  # reminders cancelled on fulfilment
    assert any(
        e.type == "completeness.updated" and e.data["complete"]
        for e in rapp.state.hub.events
        if e.case_id == cid
    )
    ev = [r[0] for r in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)]
    assert "completeness.evaluated" in ev and "doc_request.closed" in ev
    hist = (
        await rclient.get(f"/v1/cases/{cid}/completeness?history=true", headers=tok("officer1"))
    ).json()["history"]
    assert [h["run_no"] for h in hist] == sorted((h["run_no"] for h in hist), reverse=True) and len(
        hist
    ) >= 3


async def test_missing_stamp_becomes_request_then_fixed_by_reupload(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await add_doc(rclient, tok, cid, "prescription")
    bad = await add_doc(rclient, tok, cid, "pharmacy_bill", stamp=False)
    await add_doc(rclient, tok, cid, "final_bill")
    out = await comp(rclient, tok, cid)
    ph = by_rule(out)["R-PH-01"]
    assert (
        ph["status"] == "unusable"
        and ph["reasons"] == ["stamp_missing"]
        and "stamped copy" in ph["message"]
    )
    reqs = (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    assert [(r["rule_id"], r["reason_code"]) for r in reqs] == [
        ("R-PH-01", "stamp_missing")
    ]  # a request, never a rejection
    due = reqs[0]["due_by"]
    # re-upload a stamped copy as a new version
    r = await rclient.post(
        f"/v1/cases/{cid}/documents",
        headers=tok("desk1"),
        data={"doc_type_hint": "pharmacy_bill", "supersedes_id": bad},
        files=files(("ph2.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
    )
    new = r.json()["documents"][0]["id"]
    for pass_no, eng, who in ((1, "mineru-pipeline", "svc:n8n"), (2, "crew-llm", "svc:crew")):
        body = {
            "pass_no": pass_no,
            "engine": eng,
            "confidence": 0.9,
            "typed_json": {"date": "2026-09-30", "total": "10", "lines": [{"amount": "10"}]},
        }
        await rclient.post(f"/v1/internal/documents/{new}/parse", json=body, headers=tok(who))
    await rclient.post(
        f"/v1/internal/documents/{new}/quality",
        json={"quality_score": 0.9, "flags": [], "has_required_stamp": True},
        headers=tok("svc:n8n"),
    )
    out = await comp(rclient, tok, cid)
    assert out["complete"] and by_rule(out)["R-PH-01"]["document_ids"] == [new]
    assert (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ] == []
    assert due  # the original request carried a due date


async def test_same_problem_keeps_request_and_due_date(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await comp(rclient, tok, cid)
    before = (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    r = await rclient.post(
        f"/v1/cases/{cid}/completeness/run", headers=tok("officer1")
    )  # manual run, same result
    assert r.status_code == 200
    after = (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    assert [(x["id"], x["due_by"]) for x in before] == [
        (x["id"], x["due_by"]) for x in after
    ]  # not reset, not duplicated
    runs = sql(settings, "SELECT count(*) FROM completeness_check WHERE case_id=:c", c=cid)[0][0]
    assert runs == 2  # manual runs always record


async def test_unchanged_automatic_runs_do_not_spam_history(
    rclient: httpx.AsyncClient, tok: Any, rapp: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await comp(rclient, tok, cid)
    for _ in range(3):
        await rapp.state.completeness(cid, "doc_event")
    assert (
        sql(settings, "SELECT count(*) FROM completeness_check WHERE case_id=:c", c=cid)[0][0] == 1
    )


async def test_ready_for_review_regresses_and_invalidates_signoff(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    docs = [
        await add_doc(rclient, tok, cid, t) for t in ("prescription", "pharmacy_bill", "final_bill")
    ]
    assert (await comp(rclient, tok, cid))["complete"]
    from tests.test_documents import force_status

    force_status(settings, cid, "ready_for_review")
    owner = sync_engine(settings, owner=True)
    with owner.begin() as c:
        user = c.execute(text("SELECT created_by FROM claim_case WHERE id=:i"), {"i": cid}).scalar()
        draft = uuid.uuid4()
        c.execute(
            text(
                "INSERT INTO claim_draft (id, case_id, version, payload, source, created_by) VALUES (:i, :c, 1, '{}', 'agent', 'x')"
            ),
            {"i": draft, "c": cid},
        )
        c.execute(
            text(
                "INSERT INTO signoff (id, case_id, draft_id, officer_id, decision) VALUES (uuid_generate_v7(), :c, :d, :u, 'approved')"
            ),
            {"c": cid, "d": draft, "u": user},
        )
    r = await rclient.delete(
        f"/v1/documents/{docs[2]}", headers=tok("desk1")
    )  # delete the final bill
    assert r.status_code == 204
    assert (await rclient.get(f"/v1/cases/{cid}", headers=tok("officer1"))).json()[
        "status"
    ] == "docs_pending"
    assert (
        sql(settings, "SELECT invalidated_at IS NOT NULL FROM signoff WHERE case_id=:c", c=cid)[0][
            0
        ]
        is True
    )
    assert "signoff.invalidated" in [
        r[0] for r in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    out = await comp(rclient, tok, cid)
    assert by_rule(out)["R-BILL-01"]["status"] == "missing"


async def test_quality_and_review_statuses_from_pipeline(
    rclient: httpx.AsyncClient, tok: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await add_doc(rclient, tok, cid, "prescription", conf=0.62)  # below the rule minimum (0.80)
    await add_doc(rclient, tok, cid, "pharmacy_bill", flags=["blurry"])
    await add_doc(rclient, tok, cid, "final_bill")
    r = by_rule(await comp(rclient, tok, cid))
    assert (r["R-RX-01"]["status"], r["R-RX-01"]["reasons"]) == (
        "needs_review",
        ["low_parse_confidence"],
    )
    assert (r["R-PH-01"]["status"], r["R-PH-01"]["reasons"][0]) == ("unusable", "blurry")
    assert r["R-BILL-01"]["status"] == "present_ok"


async def test_unclassified_document_blocks_until_reclassified(
    rclient: httpx.AsyncClient, tok: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    for t in ("prescription", "pharmacy_bill", "final_bill"):
        await add_doc(rclient, tok, cid, t)
    up = await rclient.post(
        f"/v1/cases/{cid}/documents",
        headers=tok("desk1"),
        files=files(("mystery.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
    )
    mystery = up.json()["documents"][0]["id"]
    await rclient.post(
        f"/v1/internal/documents/{mystery}/classify",
        json={"doc_type": None, "confidence": 0.2},
        headers=tok("svc:crew"),
    )
    out = await comp(rclient, tok, cid)
    assert not out["complete"] and by_rule(out)["R-CLS-01"]["document_ids"] == [mystery]
    r = await rclient.patch(
        f"/v1/documents/{mystery}", json={"doc_type": "lab_report"}, headers=tok("desk1")
    )
    assert r.status_code == 200
    out = await comp(rclient, tok, cid)
    assert "R-CLS-01" not in by_rule(out)


async def test_waive_flow(rclient: httpx.AsyncClient, tok: Any, settings: Any) -> None:
    case = await new_case(rclient, tok("officer1"), U(), procedure_codes=["0FT44ZZ"])
    cid = case["id"]
    for t in ("prescription", "pharmacy_bill", "final_bill"):
        await add_doc(rclient, tok, cid, t)
    out = await comp(rclient, tok, cid)
    assert (
        by_rule(out)["R-PROC-01"]["status"] == "missing" and not out["complete"]
    )  # surgery -> procedure bill needed
    url = f"/v1/cases/{cid}/requirements/procedure_bill/waive"
    reason = "Procedure charges are part of the final bill; no separate procedure bill is issued"
    assert (
        await rclient.post(url, json={"reason": reason}, headers=tok("desk1"))
    ).status_code == 403
    assert (await rclient.post(url, json={"reason": "too short"}, headers=tok("officer1"))).json()[
        "code"
    ] == "reason_too_short"
    r = await rclient.post(
        f"/v1/cases/{cid}/requirements/prescription/waive",
        json={"reason": reason},
        headers=tok("officer1"),
    )
    assert r.status_code == 409 and r.json()["code"] == "not_waivable"
    r = await rclient.post(
        f"/v1/cases/{cid}/requirements/discharge_summary/waive",
        json={"reason": reason},
        headers=tok("officer1"),
    )
    assert r.status_code == 404 and r.json()["code"] == "unknown_rule"
    r = await rclient.post(url, json={"reason": reason}, headers=tok("officer1"))
    assert (
        r.status_code == 200
        and r.json()["rule_id"] == "R-PROC-01"
        and r.json()["status"] == "waived"
    )
    out = await comp(rclient, tok, cid)
    assert out["complete"] and by_rule(out)["R-PROC-01"]["status"] == "waived"
    assert "requirement.waived" in [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    assert (await rclient.get(f"/v1/cases/{cid}", headers=tok("officer1"))).json()[
        "status"
    ] == "docs_complete"
    r = await rclient.delete(
        url, headers=tok("officer1")
    )  # revoke -> original status restored, request reopened
    assert r.status_code == 200
    out = await comp(rclient, tok, cid)
    assert by_rule(out)["R-PROC-01"]["status"] == "missing" and not out["complete"]
    reqs = (await rclient.get(f"/v1/cases/{cid}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ]
    assert [x["rule_id"] for x in reqs] == ["R-PROC-01"]
    assert (await rclient.delete(url, headers=tok("officer1"))).status_code == 404


async def test_remind_rate_limited(rclient: httpx.AsyncClient, tok: Any, rapp: Any) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    await comp(rclient, tok, case["id"])
    rid = (await rclient.get(f"/v1/cases/{case['id']}/doc-requests", headers=tok("desk1"))).json()[
        "items"
    ][0]["id"]
    await rapp.state.redis.delete(f"cache:hosp:remind:{rid}")
    assert (
        await rclient.post(
            f"/v1/cases/{case['id']}/doc-requests/{rid}/remind", headers=tok("desk1")
        )
    ).status_code == 200
    r = await rclient.post(
        f"/v1/cases/{case['id']}/doc-requests/{rid}/remind", headers=tok("desk1")
    )
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert (
        await rclient.post(
            f"/v1/cases/{case['id']}/doc-requests/{uuid.uuid4()}/remind", headers=tok("desk1")
        )
    ).status_code == 404
    assert (
        await rclient.post(f"/v1/cases/{case['id']}/doc-requests/nope/remind", headers=tok("desk1"))
    ).status_code == 404


async def test_internal_run_and_authz(rclient: httpx.AsyncClient, tok: Any) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    r = await rclient.post(
        f"/v1/internal/cases/{case['id']}/completeness/run", headers=tok("svc:n8n")
    )
    assert r.status_code == 200 and r.json()["run_no"] == 1
    assert (
        await rclient.post(
            f"/v1/internal/cases/{case['id']}/completeness/run", headers=tok("svc:crew")
        )
    ).status_code == 403
    assert (
        await rclient.post(
            f"/v1/internal/cases/{case['id']}/completeness/run", headers=tok("officer1")
        )
    ).status_code == 403
    assert (
        await rclient.post(
            f"/v1/internal/cases/{uuid.uuid4()}/completeness/run", headers=tok("svc:n8n")
        )
    ).status_code == 404
    assert (
        await rclient.get(f"/v1/cases/{uuid.uuid4()}/completeness", headers=tok("officer1"))
    ).status_code == 404


async def test_debounce_collapses_bursts(
    rclient: httpx.AsyncClient, tok: Any, rapp: Any, settings: Any
) -> None:
    case = await new_case(rclient, tok("officer1"), U())
    cid = case["id"]
    await rapp.state.redis.delete(
        f"cache:hosp:completeness:debounce:{cid}", f"cache:hosp:completeness:debounce:{cid}:dirty"
    )
    s = CompletenessScheduler(rapp)
    rapp.state.settings = rapp.state.settings.model_copy(update={"completeness_debounce_s": 0.3})
    try:
        for _ in range(6):
            await s.schedule(cid, "doc_event")
        assert len(s.tasks) == 1  # only the first caller enqueued a run
        await s.drain()
        await asyncio.sleep(0.5)
        await s.drain()
    finally:
        rapp.state.settings = rapp.state.settings.model_copy(update={"completeness_debounce_s": 0})
    runs = sql(settings, "SELECT count(*) FROM completeness_check WHERE case_id=:c", c=cid)[0][0]
    assert (
        1 <= runs <= 1
    )  # dirty re-run found an unchanged result, so history did not grow beyond one row


async def test_decide_status_table() -> None:
    from app.completeness.schemas import Item, Result
    from app.completeness.service import decide_status

    ok = Result(complete=True, provisional=False, items=[])
    bad = Result(
        complete=False,
        provisional=False,
        items=[Item(rule_id="R", status="missing", severity="blocker")],
    )
    assert decide_status("docs_pending", ok, True) == "docs_complete"
    assert decide_status("docs_pending", ok, False) is None  # no documents: never "complete"
    assert decide_status("docs_complete", bad, True) == "docs_pending"
    assert decide_status("ready_for_review", bad, True) == "docs_pending"
    assert decide_status("ready_for_review", ok, True) is None
    for st in ("draft", "building_claim", "submitted", "closed"):
        assert decide_status(st, bad, True) is None


# ------------------------------------------------------------------------------------- config admin
def candidate() -> dict[str, Any]:
    cfg = deepcopy(DOC_REQUIREMENTS)
    cfg["rules"].append(
        {
            "id": "R-EMG-01",
            "doc_type": "admission_note",
            "applies": {"admission_type": ["emergency"]},
            "requirement": "conditional",
            "min_parse_confidence": 0.7,
        }
    )
    return cfg


async def test_config_admin_lifecycle(rclient: httpx.AsyncClient, tok: Any, settings: Any) -> None:
    a, b = tok("hadmin"), tok("hadmin2")
    assert (
        await rclient.get("/v1/admin/config/doc_requirements", headers=tok("officer1"))
    ).status_code == 403
    versions = (await rclient.get("/v1/admin/config/doc_requirements", headers=a)).json()[
        "versions"
    ]
    start = max(v["version"] for v in versions)
    assert (await rclient.get("/v1/admin/config/nonsense", headers=a)).status_code == 404
    assert (
        "properties"
        in (await rclient.get("/v1/admin/config/doc_requirements/schema", headers=a)).json()
    )
    # schema-invalid payloads cannot even be drafted
    r = await rclient.post(
        "/v1/admin/config/doc_requirements",
        json={"payload": {"rules": [{"id": "A"}]}, "change_note": "bad"},
        headers=a,
    )
    assert r.status_code == 422 and r.json()["code"] == "config_invalid"
    # semantically invalid draft is stored but cannot validate or publish
    bad = candidate()
    bad["rules"].append(deepcopy(bad["rules"][0]))
    r = await rclient.post(
        "/v1/admin/config/doc_requirements",
        json={"payload": bad, "change_note": "duplicate ids"},
        headers=a,
    )
    assert r.status_code == 201 and r.json()["semantic_errors"]
    bad_v = r.json()["version"]
    v = (
        await rclient.post(f"/v1/admin/config/doc_requirements/{bad_v}/validate", headers=a)
    ).json()
    assert v["valid"] is False and any("duplicate" in e for e in v["errors"])
    r = await rclient.post(f"/v1/admin/config/doc_requirements/{bad_v}/publish", headers=b)
    assert r.status_code == 422 and r.json()["code"] == "config_invalid"
    # good draft: dry-run, validate, two-person publish
    r = await rclient.post(
        "/v1/admin/config/doc_requirements",
        json={"payload": candidate(), "change_note": "emergency note"},
        headers=a,
    )
    ver = r.json()["version"]
    assert ver > bad_v > start
    assert (
        await rclient.post(f"/v1/admin/config/doc_requirements/{ver}/validate", headers=a)
    ).json() == {"valid": True, "errors": []}
    emergency = await new_case(
        rclient, tok("officer1"), U(), admission_type="emergency", preauth_ref=None
    )
    d = (
        await rclient.post(
            f"/v1/admin/config/doc_requirements/{ver}/dry-run",
            json={"sample": "last_100_cases"},
            headers=a,
        )
    ).json()
    assert d["evaluated"] >= 1 and d["changed"] >= 1
    assert any(
        x["rule_id"] == "R-EMG-01" and x["case_ref"] == emergency["claim_ref"]
        for x in d["newly_blocked"]
    )
    assert d["by_rule"]["R-EMG-01"]["newly_blocked"] >= 1
    r = await rclient.post(f"/v1/admin/config/doc_requirements/{ver}/publish", headers=a)
    assert (
        r.status_code == 409 and r.json()["code"] == "two_person_rule"
    )  # creator cannot publish alone
    r = await rclient.post(f"/v1/admin/config/doc_requirements/{ver}/publish", headers=b)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert (
        await rclient.post(f"/v1/admin/config/doc_requirements/{ver}/publish", headers=b)
    ).status_code == 409  # not a draft anymore
    vs = {
        x["version"]: x
        for x in (await rclient.get("/v1/admin/config/doc_requirements", headers=a)).json()[
            "versions"
        ]
    }
    assert (
        vs[ver]["status"] == "published"
        and vs[ver]["second_approver"]
        and vs[ver]["published_by"] != vs[ver]["second_approver"]
    )
    prev = vs[start]
    assert (
        prev["effective_to"] is not None
    )  # previous version's window closed at the new version's start
    # new cases snapshot the new version; existing cases keep theirs until re-evaluated
    old_case = emergency
    new_case_ = await new_case(
        rclient, tok("officer1"), U(), admission_type="emergency", preauth_ref=None
    )

    def cfg_of(cid: str) -> Any:
        return sql(
            settings,
            "SELECT config_versions->>'doc_requirements' FROM claim_case WHERE id=:i",
            i=cid,
        )[0][0]

    assert int(cfg_of(new_case_["id"])) == ver and int(cfg_of(old_case["id"])) < ver
    out = await comp(rclient, tok, new_case_["id"])
    assert "R-EMG-01" in by_rule(out)
    assert "R-EMG-01" not in by_rule(await comp(rclient, tok, old_case["id"]))
    r = await rclient.post(
        "/v1/admin/config/doc_requirements/reevaluate",
        json={"case_ids": [old_case["id"]]},
        headers=a,
    )
    assert (
        r.status_code == 200
        and r.json()["re_evaluated"] == 1
        and r.json()["doc_requirements_version"] == ver
    )
    assert int(cfg_of(old_case["id"])) == ver and "R-EMG-01" in by_rule(
        await comp(rclient, tok, old_case["id"])
    )
    ev = [
        x[0]
        for x in sql(
            settings,
            "SELECT event_type FROM audit_event WHERE case_id='00000000-0000-0000-0000-000000000000' ORDER BY seq",
        )
    ]
    assert {"config.drafted", "config.validated", "config.published"} <= set(ev)
    # the other domains share the lifecycle (validation differs)
    r = await rclient.post(
        "/v1/admin/config/deadlines",
        json={"payload": {"reimbursement_filing_days": -1}, "change_note": "bad"},
        headers=a,
    )
    assert r.status_code == 422
    r = await rclient.get("/v1/admin/config/doc_requirements/999", headers=a)
    assert r.status_code == 404
    r = await rclient.post("/v1/admin/config/confidence_gates/1/dry-run", headers=a)
    assert r.status_code in (404, 422)


async def test_published_config_cannot_be_edited_in_db(settings: Any) -> None:
    eng = create_engine(settings.db_url.replace("+asyncpg", "+psycopg"))
    with eng.connect() as c, pytest.raises(Exception, match="immutable|permission"):
        c.execute(text("UPDATE config_version SET payload='{}' WHERE status='published'"))
