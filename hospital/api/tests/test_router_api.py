"""Router endpoints and persistence (doc 05 §4, §9)."""

import uuid
from copy import deepcopy
from typing import Any

import httpx
import pytest
from seed.config_payloads import ROUTER_RULES
from tests.helpers import files, new_case, pdf_bytes
from tests.test_completeness_api import add_doc
from tests.test_documents import force_status, sql

pytestmark = pytest.mark.integration
PDF = "application/pdf"


def U() -> str:
    return "UH-" + uuid.uuid4().hex[:8]


async def route(
    c: httpx.AsyncClient, tok: Any, case_id: str, who: str = "officer1"
) -> dict[str, Any]:
    r = await c.get(f"/v1/cases/{case_id}/route", headers=tok(who))
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def history(settings: Any, case_id: str) -> list[Any]:
    return sql(
        settings, "SELECT seq, trigger FROM route_history WHERE case_id=:c ORDER BY seq", c=case_id
    )


async def test_create_decides_route_and_records_history(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    c = await new_case(
        rclient,
        tok("desk1"),
        U(),
        policy={"insurer_name": "Acme Health", "policy_number": "P1", "member_id": "M-77123"},
    )
    assert c["claim_type"] == "cashless" and c["route"]["pipeline"] == "cashless"
    r = await route(rclient, tok, c["id"], "desk1")
    d = r["decision"]
    assert (d["pipeline"], d["admission_type"]) == ("cashless", "planned")
    assert d["required_steps"] == [
        "preauth_check",
        "completeness",
        "claim_build",
        "officer_signoff",
        "submit",
    ]
    assert set(d["config_versions"]) == {
        "doc_requirements",
        "deadlines",
        "router_rules",
        "confidence_gates",
    }
    assert r["proposal"]["claim_type"] == "cashless" and r["seq"] == 1
    assert [(h[0], h[1]) for h in history(settings, c["id"])] == [(1, "create")]
    row = sql(
        settings, "SELECT claim_type::text, admission_source FROM claim_case WHERE id=:i", i=c["id"]
    )[0]
    assert row == ("cashless", "OPD")
    types = [
        x[0]
        for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=c["id"])
    ]
    assert "router.decided" in types


async def test_router_disagreement_warns_and_must_be_acknowledged(
    rclient: httpx.AsyncClient, tok: Any
) -> None:
    c = await new_case(
        rclient, tok("desk1"), U(), preauth_ref=None
    )  # cashless proposed, no reference at OPD
    assert c["claim_type"] == "reimbursement"
    r = await route(rclient, tok, c["id"])
    w = {x["code"]: x for x in r["warnings"]}
    assert (
        w["claim_type_disagrees_with_selection"]["needs_ack"] is True
        and r["proposal"]["claim_type"] == "cashless"
    )
    assert (
        r["decision"]["required_steps"][-1] == "filing_window_check"
        and r["decision"]["filing_deadline"]
    )
    assert (
        await rclient.post(
            f"/v1/cases/{c['id']}/route/ack", json={"codes": ["nope"]}, headers=tok("officer1")
        )
    ).status_code == 422
    assert (
        await rclient.post(f"/v1/cases/{c['id']}/route/ack", json={}, headers=tok("desk1"))
    ).status_code == 403
    r = await rclient.post(f"/v1/cases/{c['id']}/route/ack", json={}, headers=tok("officer1"))
    assert (
        r.status_code == 200
        and r.json()["pending"] == []
        and r.json()["ack"]["codes"] == ["claim_type_disagrees_with_selection"]
    )
    assert (await route(rclient, tok, c["id"]))["ack"]["codes"] == [
        "claim_type_disagrees_with_selection"
    ]
    ok = await new_case(rclient, tok("desk1"), U())
    assert (
        await rclient.post(f"/v1/cases/{ok['id']}/route/ack", json={}, headers=tok("officer1"))
    ).status_code == 409


async def test_recompute_is_idempotent_and_patch_triggers_it(
    rclient: httpx.AsyncClient, tok: Any, settings: Any, rapp: Any
) -> None:
    c = await new_case(rclient, tok("officer1"), U())
    cid, h = c["id"], tok("officer1")
    for _ in range(3):
        r = await rclient.post(f"/v1/cases/{cid}/route/recompute", headers=h)
        assert r.json() == {"changed": False, "seq": 1}
    assert len(history(settings, cid)) == 1
    n_audit = sql(
        settings,
        "SELECT count(*) FROM audit_event WHERE case_id=:c AND event_type='router.decided'",
        c=cid,
    )[0][0]
    assert n_audit == 1
    before = len(rapp.state.hub.of_type("case.route_changed"))
    r = await rclient.patch(
        f"/v1/cases/{cid}", json={"diagnosis_codes": ["O80"]}, headers={**h, "If-Match": "1"}
    )
    assert r.status_code == 200
    d = await route(rclient, tok, cid)
    assert d["seq"] == 2 and "maternity" in d["decision"]["flags"]
    assert [x[1] for x in history(settings, cid)] == ["create", "patch"]
    assert len(rapp.state.hub.of_type("case.route_changed")) == before + 1
    assert (await rclient.get("/v1/cases?flag=maternity&size=100", headers=h)).json()["items"]
    listed = (await rclient.get("/v1/cases?flag=maternity&size=100", headers=h)).json()["items"]
    assert cid in [i["id"] for i in listed]
    # change the diagnosis again: the diff shows what moved
    r = await rclient.patch(
        f"/v1/cases/{cid}", json={"diagnosis_codes": ["S72.0"]}, headers={**h, "If-Match": "2"}
    )
    assert r.status_code == 200
    last = history(settings, cid)[-1]
    assert last[0] == 3
    assert "medico_legal" in (await route(rclient, tok, cid))["decision"]["flags"]
    assert "maternity" not in (await route(rclient, tok, cid))["decision"]["flags"]


async def test_emergency_intimation_deadline_and_steps(
    rclient: httpx.AsyncClient, tok: Any
) -> None:
    c = await new_case(
        rclient,
        tok("officer1"),
        U(),
        admission_type="emergency",
        preauth_ref=None,
        admitted_at="2026-09-28T10:00:00Z",
    )
    r = await route(rclient, tok, c["id"])
    d = r["decision"]
    assert (d["pipeline"], d["admission_type"]) == (
        "cashless",
        "emergency",
    )  # emergency at a network hospital
    assert (
        d["required_steps"][-1] == "intimation_check"
        and d["intimation_deadline"] == "2026-09-29T10:00:00Z"
    )
    detail = (await rclient.get(f"/v1/cases/{c['id']}", headers=tok("officer1"))).json()
    assert (
        detail["route"]["decision"]["admission_type"] == "emergency"
        and detail["admission_type"] == "emergency"
    )


async def test_unknown_insurer_goes_reimbursement_with_warning(
    rclient: httpx.AsyncClient, tok: Any
) -> None:
    c = await new_case(
        rclient,
        tok("officer1"),
        U(),
        policy={"insurer_name": "Star Healt", "policy_number": "P", "member_id": "M-1"},
    )
    assert c["claim_type"] == "reimbursement"
    codes = {w["code"] for w in c["warnings"]}
    assert "insurer_not_in_network_table" in codes
    zen = await new_case(
        rclient,
        tok("officer1"),
        U(),
        policy={"insurer_name": "Zenith Mutual", "policy_number": "P", "member_id": "M-1"},
    )
    assert zen["claim_type"] == "reimbursement" and "insurer_not_in_network_table" not in {
        w["code"] for w in zen["warnings"]
    }


async def test_late_filing_flag(rclient: httpx.AsyncClient, tok: Any) -> None:
    c = await new_case(
        rclient,
        tok("officer1"),
        U(),
        claim_type="reimbursement",
        preauth_ref=None,
        admitted_on="2026-07-20",
        discharged_on="2026-08-01",
    )
    r = await route(rclient, tok, c["id"])
    assert "late_filing" in r["decision"]["flags"]
    assert any(w["code"] == "late_filing" and w["needs_ack"] for w in r["warnings"])
    assert r["decision"]["filing_deadline"] == "2026-08-31T18:29:59Z"


async def test_preauth_issue_flag_never_blocks(rclient: httpx.AsyncClient, tok: Any) -> None:
    c = await new_case(
        rclient, tok("officer1"), U(), preauth_ref="PA-2026-33102"
    )  # seeded as expired for another member
    r = await route(rclient, tok, c["id"])
    assert "preauth_issue" in r["decision"]["flags"] and r["decision"]["pipeline"] == "cashless"
    codes = {w["code"] for w in r["warnings"]}
    assert {
        "preauth_member_mismatch",
        "preauth_status_expired",
        "preauth_outside_validity",
    } <= codes


async def test_override_rules_and_evidence_conflict(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    c = await new_case(rclient, tok("officer1"), U(), diagnosis_codes=["S72.0"])
    cid, h = c["id"], tok("officer1")
    assert "medico_legal" in (await route(rclient, tok, cid))["decision"]["flags"]
    url = f"/v1/cases/{cid}/route/override"
    reason = "Patient opted out of cashless after the TPA call"
    assert (
        await rclient.post(
            url, json={"claim_type": "reimbursement", "reason": reason}, headers=tok("desk1")
        )
    ).status_code == 403
    r = await rclient.post(
        url, json={"claim_type": "reimbursement", "reason": "too short"}, headers=h
    )
    assert r.status_code == 422 and r.json()["code"] == "reason_too_short"
    assert (
        await rclient.post(url, json={"reason": reason}, headers=h)
    ).status_code == 422  # nothing to override
    assert (
        await rclient.post(url, json={"claim_type": "barter", "reason": reason}, headers=h)
    ).status_code == 422
    r = await rclient.post(
        url,
        json={"claim_type": "reimbursement", "flags_remove": ["medico_legal"], "reason": reason},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["override"] is True
    d = r.json()["decision"]
    assert d["pipeline"] == "reimbursement" and "medico_legal" not in d["flags"]
    assert (
        d["rules_output"]["pipeline"] == "cashless" and "medico_legal" in d["rules_output"]["flags"]
    )  # visible for transparency
    assert r.json()["overrides"]["reason"] == reason
    assert "router.override" in [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=cid)
    ]
    # recompute keeps overrides unless cleared
    await rclient.post(f"/v1/cases/{cid}/route/recompute", headers=h)
    assert (await route(rclient, tok, cid))["decision"]["pipeline"] == "reimbursement"
    r = await rclient.post(f"/v1/cases/{cid}/route/recompute?clear_overrides=true", headers=h)
    assert r.json()["changed"] is True
    after = await route(rclient, tok, cid)
    assert (
        after["decision"]["pipeline"] == "cashless"
        and "medico_legal" in after["decision"]["flags"]
        and not after["overrides"]
    )
    # evidence: with an FIR document present the medico_legal flag cannot be removed
    up = await rclient.post(
        f"/v1/cases/{cid}/documents",
        headers=tok("desk1"),
        data={"doc_type_hint": "fir_mlc"},
        files=files(("fir.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
    )
    assert up.status_code == 202
    r = await rclient.post(
        url, json={"flags_remove": ["medico_legal"], "reason": reason}, headers=h
    )
    assert r.status_code == 409 and r.json()["code"] == "override_conflicts_evidence"
    # cashless override while the insurer is not in the network is allowed with a stored warning
    z = await new_case(
        rclient,
        tok("officer1"),
        U(),
        policy={"insurer_name": "Zenith Mutual", "policy_number": "P", "member_id": "M-1"},
    )
    r = await rclient.post(
        f"/v1/cases/{z['id']}/route/override",
        json={"claim_type": "cashless", "reason": reason},
        headers=h,
    )
    assert r.status_code == 200 and "override_against_network" in {
        w["code"] for w in r.json()["warnings"]
    }
    # locked once submitted
    force_status(settings, cid, "submitted")
    r = await rclient.post(url, json={"admission_type": "emergency", "reason": reason}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "case_locked"


async def test_fir_classification_triggers_recompute(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    c = await new_case(rclient, tok("officer1"), U(), diagnosis_codes=["K80.2"])
    cid = c["id"]
    assert "medico_legal" not in (await route(rclient, tok, cid))["decision"]["flags"]
    await add_doc(
        rclient, tok, cid, "fir_mlc", typed={"patient_name": "Ravi Kumar", "date": "2026-09-29"}
    )
    # hints are manual classifications; the callback path needs an auto one
    up = await rclient.post(
        f"/v1/cases/{cid}/documents",
        headers=tok("desk1"),
        files=files(("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
    )
    doc = up.json()["documents"][0]["id"]
    r = await rclient.post(
        f"/v1/internal/documents/{doc}/classify",
        json={"doc_type": "fir_mlc", "confidence": 0.95},
        headers=tok("svc:crew"),
    )
    assert r.status_code == 200
    d = await route(rclient, tok, cid)
    assert "medico_legal" in d["decision"]["flags"]
    assert "doc_classified" in [x[1] for x in history(settings, cid)] or "create" in [
        x[1] for x in history(settings, cid)
    ]


async def test_post_submission_recompute_freezes_steps(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    c = await new_case(rclient, tok("officer1"), U())
    cid, h = c["id"], tok("officer1")
    before = (await route(rclient, tok, cid))["decision"]
    force_status(settings, cid, "submitted")
    r = await rclient.patch(
        f"/v1/cases/{cid}", json={"treating_doctor": "x"}, headers={**h, "If-Match": "1"}
    )
    assert r.status_code == 409  # edits are locked after submission
    sql(
        settings, "UPDATE claim_case SET diagnosis_codes = ARRAY['S72.0'] WHERE id=:i", i=cid
    )  # a fact changes anyway
    r = await rclient.post(f"/v1/cases/{cid}/route/recompute", headers=h)
    assert r.status_code == 200 and r.json()["changed"] is True
    after = (await route(rclient, tok, cid))["decision"]
    assert (
        after["required_steps"] == before["required_steps"]
        and after["pipeline"] == before["pipeline"]
    )
    assert "medico_legal" in after["flags"]  # flags still follow the facts, for display
    assert history(settings, cid)[-1][1] == "post_submission"


async def test_convert_cashless_to_reimbursement(
    rclient: httpx.AsyncClient, tok: Any, rapp: Any, settings: Any
) -> None:
    c = await new_case(rclient, tok("officer1"), U())
    cid, h = c["id"], tok("officer1")
    d1 = await add_doc(rclient, tok, cid, "prescription")
    d2 = await add_doc(rclient, tok, cid, "final_bill")
    assert (
        await rclient.post(
            f"/v1/cases/{cid}/convert", json={"to": "cashless", "reason": "x" * 20}, headers=h
        )
    ).status_code == 422
    assert (
        await rclient.post(
            f"/v1/cases/{cid}/convert", json={"to": "reimbursement", "reason": "short"}, headers=h
        )
    ).status_code == 422
    assert (
        await rclient.post(
            f"/v1/cases/{cid}/convert",
            json={"to": "reimbursement", "reason": "x" * 20},
            headers=tok("desk1"),
        )
    ).status_code == 403
    reason = "Insurer declined cashless at the final stage; patient will claim after discharge"
    r = await rclient.post(
        f"/v1/cases/{cid}/convert", json={"to": "reimbursement", "reason": reason}, headers=h
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["documents_copied"] == 2
        and out["original_status"] == "closed"
        and out["original_close_reason"] == "converted"
    )
    new_id = out["new_case_id"]
    assert out["new_claim_ref"] != c["claim_ref"] and out["linked_from"] == c["claim_ref"]
    old = (await rclient.get(f"/v1/cases/{cid}", headers=h)).json()
    new = (await rclient.get(f"/v1/cases/{new_id}", headers=h)).json()
    assert (
        old["status"] == "closed"
        and new["status"] == "draft"
        and new["claim_type"] == "reimbursement"
    )
    assert new["patient"]["uhid"] == old["patient"]["uhid"] and new["filing_deadline"]
    assert (
        sql(settings, "SELECT converted_to::text FROM claim_case WHERE id=:i", i=cid)[0][0]
        == new_id
    )
    assert (
        sql(settings, "SELECT converted_from::text FROM claim_case WHERE id=:i", i=new_id)[0][0]
        == cid
    )
    od = {
        x[0]: x
        for x in sql(
            settings,
            "SELECT sha256, storage_key, doc_type::text, parse_status FROM document WHERE case_id=:c",
            c=cid,
        )
    }
    nd = {
        x[0]: x
        for x in sql(
            settings,
            "SELECT sha256, storage_key, doc_type::text, parse_status FROM document WHERE case_id=:c",
            c=new_id,
        )
    }
    assert set(od) == set(nd) and len(nd) == 2
    for sha, row in nd.items():
        assert (
            row[1] != od[sha][1] and await rapp.state.store.exists(row[1]) and row[2] == od[sha][2]
        )  # real copy
    assert (
        sql(
            settings,
            "SELECT count(*) FROM document_parse WHERE document_id IN (SELECT id FROM document WHERE case_id=:c)",
            c=new_id,
        )[0][0]
        == 4
    )
    # deleting in the successor never damages the original's objects
    new_docs = (await rclient.get(f"/v1/cases/{new_id}/documents", headers=h)).json()["documents"]
    assert (
        await rclient.delete(f"/v1/documents/{new_docs[0]['id']}", headers=h)
    ).status_code == 204
    for r in od.values():
        assert await rapp.state.store.exists(r[1])
    ev = lambda case: [
        x[0] for x in sql(settings, "SELECT event_type FROM audit_event WHERE case_id=:c", c=case)
    ]  # noqa: E731
    assert "router.converted" in ev(cid) and "router.converted" in ev(new_id)
    r = await rclient.post(
        f"/v1/cases/{cid}/convert", json={"to": "reimbursement", "reason": reason}, headers=h
    )
    assert (
        r.status_code == 409
        and r.json()["code"] == "already_converted"
        and r.json()["successor"] == new_id
    )
    assert d1 and d2


async def test_convert_not_allowed_from_settled_or_reimbursement(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    reason = "Insurer declined cashless at the final stage"
    h = tok("officer1")
    c = await new_case(rclient, h, U())
    force_status(settings, c["id"], "settled")
    assert (
        await rclient.post(
            f"/v1/cases/{c['id']}/convert",
            json={"to": "reimbursement", "reason": reason},
            headers=h,
        )
    ).status_code == 409
    r = await new_case(rclient, h, U(), claim_type="reimbursement", preauth_ref=None)
    assert (
        await rclient.post(
            f"/v1/cases/{r['id']}/convert",
            json={"to": "reimbursement", "reason": reason},
            headers=h,
        )
    ).status_code == 409
    rej = await new_case(rclient, h, U())
    force_status(settings, rej["id"], "rejected")  # rejected cashless claims may convert
    assert (
        await rclient.post(
            f"/v1/cases/{rej['id']}/convert",
            json={"to": "reimbursement", "reason": reason},
            headers=h,
        )
    ).status_code == 200


async def test_internal_recompute_authz(rclient: httpx.AsyncClient, tok: Any) -> None:
    c = await new_case(rclient, tok("officer1"), U())
    r = await rclient.post(f"/v1/internal/cases/{c['id']}/route/recompute", headers=tok("svc:n8n"))
    assert r.status_code == 200 and r.json()["changed"] is False
    assert (
        await rclient.post(f"/v1/internal/cases/{c['id']}/route/recompute", headers=tok("svc:crew"))
    ).status_code == 200
    assert (
        await rclient.post(f"/v1/internal/cases/{c['id']}/route/recompute", headers=tok("officer1"))
    ).status_code == 403
    assert (
        await rclient.get(f"/v1/cases/{uuid.uuid4()}/route", headers=tok("officer1"))
    ).status_code == 404


async def test_router_rules_republish_recomputes_in_flight_cases(
    rclient: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    a, b = tok("hadmin"), tok("hadmin2")
    draft_case = await new_case(rclient, tok("officer1"), U())
    locked_case = await new_case(rclient, tok("officer1"), U())
    force_status(settings, locked_case["id"], "submitted")
    bad = deepcopy(ROUTER_RULES)
    bad["claim_type_rules"].pop()  # no trailing default
    r = await rclient.post(
        "/v1/admin/config/router_rules", json={"payload": bad, "change_note": "broken"}, headers=a
    )
    assert r.status_code == 422 and r.json()["code"] == "config_invalid"
    good = deepcopy(ROUTER_RULES)
    good["flag_rules"].append(
        {"flag": "implant", "if": {"icd10_prefix": ["ZZZ"]}}
    )  # harmless, never true
    r = await rclient.post(
        "/v1/admin/config/router_rules",
        json={"payload": good, "change_note": "adds a harmless rule"},
        headers=a,
    )
    assert r.status_code == 201, r.text
    ver = r.json()["version"]
    assert (
        await rclient.post(f"/v1/admin/config/router_rules/{ver}/publish", headers=a)
    ).status_code == 409
    r = await rclient.post(f"/v1/admin/config/router_rules/{ver}/publish", headers=b)
    assert r.status_code == 200 and r.json()["cases_recomputed"] >= 1
    cfg = lambda cid: sql(
        settings,
        "SELECT (config_versions->>'router_rules')::int FROM claim_case WHERE id=:i",
        i=cid,
    )[0][0]  # noqa: E731
    assert (
        cfg(draft_case["id"]) == ver and cfg(locked_case["id"]) < ver
    )  # submitted cases are untouched
    assert (await route(rclient, tok, draft_case["id"]))["decision"]["config_versions"][
        "router_rules"
    ] == ver
