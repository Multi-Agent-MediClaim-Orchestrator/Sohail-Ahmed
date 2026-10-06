"""Cases API (doc 03 §4.1, §9)."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from app.services import audit as audit_svc
from claim_contract.audit import canonical_json  # noqa: F401
from sqlalchemy import create_engine, text
from tests.helpers import case_body, new_case

pytestmark = pytest.mark.integration


def U() -> str:
    return "UH-" + uuid.uuid4().hex[:8]


async def test_create_case_ok(client: httpx.AsyncClient, tok: Any, settings: Any) -> None:
    r = await client.post("/v1/cases", json=case_body(U()), headers=tok("desk1"))
    assert r.status_code == 201
    j = r.json()
    assert j["status"] == "draft" and j["version"] == 1 and j["claim_ref"].startswith("HC-")
    assert r.headers["location"] == f"/v1/cases/{j['id']}" and r.headers["etag"] == '"1"'
    assert {w["code"] for w in j["warnings"]} == {
        "preauth_member_mismatch"
    }  # seeded for another member
    d = (await client.get(f"/v1/cases/{j['id']}", headers=tok("officer1"))).json()
    assert d["config_versions"].keys() == {
        "doc_requirements",
        "deadlines",
        "router_rules",
        "confidence_gates",
    }
    assert d["patient"]["phone_last4"] == "0000" and "phone" not in d["patient"]
    assert (
        d["allowed_transitions"] == ["closed"] and d["claim_type"] == "cashless"
    )  # officer may cancel a draft
    # audit chain verifies and has no PII
    eng = create_engine(settings.db_url.replace("+asyncpg", "+psycopg"))
    with eng.connect() as c:
        ev = c.execute(
            text("SELECT event_type, payload::text FROM audit_event WHERE case_id=:i ORDER BY seq"),
            {"i": j["id"]},
        ).all()
    assert ev[0].event_type == "case.created" and "Ravi" not in ev[0].payload


async def test_audit_chain_verifies(client: httpx.AsyncClient, tok: Any, app: Any) -> None:
    c = await new_case(client, tok("desk1"), U())
    async with app.state.sessionmaker() as s:
        res = await audit_svc.verify(s, c["id"])
    assert res.ok and res.count >= 1


@pytest.mark.parametrize(
    ("name", "patch"),
    [
        ("dob_future", {"patient": {"dob": "2999-01-01"}}),
        ("dob_too_old", {"patient": {"dob": "1850-01-01"}}),
        ("bad_gender", {"patient": {"gender": "X"}}),
        ("discharge_before_admit", {"admitted_on": "2026-10-02", "discharged_on": "2026-09-01"}),
        ("discharge_future", {"discharged_on": "2999-01-01"}),
        ("empty_policy", {"policy": {"policy_number": ""}}),
        ("long_policy", {"policy": {"member_id": "x" * 65}}),
        ("extra_field", {"surprise": 1}),
        ("bad_claim_type", {"claim_type": "other"}),
    ],
)
async def test_create_validation(
    client: httpx.AsyncClient, tok: Any, name: str, patch: dict[str, Any]
) -> None:
    body = case_body(U())
    for k, v in patch.items():
        body[k] = {**body[k], **v} if isinstance(v, dict) and isinstance(body.get(k), dict) else v
    r = await client.post("/v1/cases", json=body, headers=tok("desk1"))
    assert r.status_code == 422 and r.json()["code"] == "validation_error", (name, r.text)


async def test_reimbursement_deadline_and_no_preauth_warning(
    client: httpx.AsyncClient, tok: Any
) -> None:
    c = await new_case(client, tok("officer1"), U(), claim_type="reimbursement", preauth_ref=None)
    assert c["filing_deadline"] == "2026-11-01"  # discharged 2026-10-02 + 30 days (config)
    assert c["warnings"] == []
    c2 = await new_case(client, tok("officer1"), U(), preauth_ref=None)
    # the router decides: no pre-auth reference at an OPD admission -> reimbursement, with an acknowledgement-worthy warning
    assert c2["claim_type"] == "reimbursement"
    assert [(w["code"], w["needs_ack"]) for w in c2["warnings"]] == [
        ("claim_type_disagrees_with_selection", True)
    ]
    c3 = await new_case(client, tok("officer1"), U(), preauth_ref="PA-DOES-NOT-EXIST")
    assert [w["code"] for w in c3["warnings"]] == ["preauth_not_found"]


async def test_patient_conflict_and_confirm(client: httpx.AsyncClient, tok: Any) -> None:
    uhid = U()
    await new_case(client, tok("desk1"), uhid)
    body = case_body(uhid)
    body["patient"]["full_name"] = "Someone Else"
    r = await client.post("/v1/cases", json=body, headers=tok("desk1"))
    assert r.status_code == 409 and r.json()["code"] == "patient_conflict"
    assert r.json()["stored"]["full_name"] == "Ravi Kumar"
    r = await client.post("/v1/cases?confirm_patient_update=true", json=body, headers=tok("desk1"))
    assert r.status_code == 201


async def test_rbac_create(client: httpx.AsyncClient, tok: Any) -> None:
    assert (
        await client.post("/v1/cases", json=case_body(U()), headers=tok("hadmin"))
    ).status_code == 403
    assert (
        await client.post("/v1/cases", json=case_body(U()), headers=tok("svc:n8n"))
    ).status_code == 403
    assert (await client.post("/v1/cases", json=case_body(U()))).status_code == 401


async def test_desk_scope_vs_officer(client: httpx.AsyncClient, tok: Any) -> None:
    mine = await new_case(client, tok("desk1"), U())
    other = await new_case(client, tok("officer1"), U())
    me = (await client.get("/v1/me", headers=tok("desk1"))).json()["id"]
    officer = (await client.get("/v1/me", headers=tok("officer2"))).json()["id"]
    # assign `other` to officer2 -> desk1 must no longer see it; `mine` stays unassigned (visible)
    r = await client.post(
        f"/v1/cases/{other['id']}/assign", json={"user_id": officer}, headers=tok("officer1")
    )
    assert r.status_code == 200
    ids = {
        i["id"]
        for i in (await client.get("/v1/cases?size=100", headers=tok("desk1"))).json()["items"]
    }
    assert mine["id"] in ids and other["id"] not in ids
    assert (
        await client.get(f"/v1/cases/{other['id']}", headers=tok("desk1"))
    ).status_code == 404  # no leak
    assert (
        await client.get(f"/v1/cases/{other['id']}", headers=tok("officer1"))
    ).status_code == 200
    assert (await client.get(f"/v1/cases/{other['id']}", headers=tok("hadmin"))).status_code == 200
    # desk cannot assign; assignee must be active desk/officer
    assert (
        await client.post(
            f"/v1/cases/{mine['id']}/assign", json={"user_id": me}, headers=tok("desk1")
        )
    ).status_code == 403
    admin = (await client.get("/v1/me", headers=tok("hadmin"))).json()["id"]
    r = await client.post(
        f"/v1/cases/{mine['id']}/assign", json={"user_id": admin}, headers=tok("officer1")
    )
    assert r.status_code == 422
    r = await client.post(
        f"/v1/cases/{other['id']}/assign", json={"user_id": None}, headers=tok("officer1")
    )
    assert r.status_code == 200


async def test_list_filters_search_and_keyset_stability(
    client: httpx.AsyncClient, tok: Any
) -> None:
    tag = uuid.uuid4().hex[:6]
    created = []
    for i in range(7):
        created.append(
            (
                await new_case(
                    client,
                    tok("officer1"),
                    f"UHX-{tag}-{i}",
                    patient={
                        "uhid": f"UHX-{tag}-{i}",
                        "full_name": f"Zebra{tag} Person{i}",
                        "dob": "1990-01-01",
                        "gender": "F",
                    },
                )
            )["id"]
        )
    r = await client.get(
        "/v1/cases", params={"q": f"UHX-{tag}", "size": 3}, headers=tok("officer1")
    )
    p1 = r.json()
    assert len(p1["items"]) == 3 and p1["next_cursor"]
    new = await new_case(
        client,
        tok("officer1"),
        f"UHX-{tag}-new",
        patient={
            "uhid": f"UHX-{tag}-new",
            "full_name": "Zebra New",
            "dob": "1990-01-01",
            "gender": "F",
        },
    )
    seen = [i["id"] for i in p1["items"]]
    cursor = p1["next_cursor"]
    while cursor:  # inserts between page fetches must not duplicate or drop older rows
        pg = (
            await client.get(
                "/v1/cases",
                params={"q": f"UHX-{tag}", "size": 3, "cursor": cursor},
                headers=tok("officer1"),
            )
        ).json()
        seen += [i["id"] for i in pg["items"]]
        cursor = pg["next_cursor"]
    assert len(seen) == len(set(seen)) and set(seen) == set(created) and new["id"] not in seen
    r = await client.get("/v1/cases", params={"q": f"Zebra{tag}"}, headers=tok("officer1"))
    assert len(r.json()["items"]) == 7
    r = await client.get(
        "/v1/cases", params={"status": ["docs_pending"], "q": f"UHX-{tag}"}, headers=tok("officer1")
    )
    assert r.json()["items"] == []
    assert (await client.get("/v1/cases?q=ab", headers=tok("officer1"))).status_code == 422
    assert (
        await client.get("/v1/cases?cursor=garbage", headers=tok("officer1"))
    ).status_code == 400
    assert (await client.get("/v1/cases?size=101", headers=tok("officer1"))).status_code == 422


async def test_patch_optimistic_lock(client: httpx.AsyncClient, tok: Any) -> None:
    c = await new_case(client, tok("desk1"), U())
    h = tok("desk1")
    r = await client.patch(f"/v1/cases/{c['id']}", json={"treating_doctor": "Dr. New"}, headers=h)
    assert r.status_code == 412  # If-Match required
    r = await client.patch(
        f"/v1/cases/{c['id']}", json={"treating_doctor": "Dr. New"}, headers={**h, "If-Match": "1"}
    )
    assert (
        r.status_code == 200
        and r.json()["version"] == 2
        and r.json()["treating_doctor"] == "Dr. New"
    )
    r = await client.patch(
        f"/v1/cases/{c['id']}", json={"treating_doctor": "Dr. Old"}, headers={**h, "If-Match": "1"}
    )
    assert r.status_code == 412  # stale
    r = await client.patch(
        f"/v1/cases/{c['id']}", json={"claimed_amount": "1234.50"}, headers={**h, "If-Match": '"2"'}
    )
    assert r.status_code == 200 and r.json()["claimed_amount"] == "1234.50"
    r = await client.patch(
        f"/v1/cases/{c['id']}", json={"claimed_amount": "abc"}, headers={**h, "If-Match": "3"}
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/v1/cases/{c['id']}",
        json={"patient": {"full_name": "R Kumar"}},
        headers={**h, "If-Match": "3"},
    )
    assert r.status_code == 200 and r.json()["patient"]["full_name"] == "R Kumar"
    r = await client.patch(
        f"/v1/cases/{c['id']}", json={"discharged_on": "2020-01-01"}, headers={**h, "If-Match": "4"}
    )
    assert r.status_code == 422  # before admitted_on


async def test_patch_blocked_when_locked_and_concurrent_edit(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    c = await new_case(client, tok("desk1"), U())
    h = {**tok("desk1"), "If-Match": "1"}
    rs = await asyncio.gather(
        *[
            client.patch(f"/v1/cases/{c['id']}", json={"treating_doctor": f"Dr {i}"}, headers=h)
            for i in range(4)
        ]
    )
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1 and codes.count(412) == 3  # exactly one writer wins


async def test_manual_transitions_and_timeline(client: httpx.AsyncClient, tok: Any) -> None:
    c = await new_case(client, tok("officer1"), U())
    h = tok("officer1")
    # draft -> docs_complete is not a legal contract transition
    r = await client.post(
        f"/v1/cases/{c['id']}/transition", json={"to": "docs_complete"}, headers=h
    )
    assert (
        r.status_code == 409 and r.json()["code"] == "invalid_transition" and "allowed" in r.json()
    )
    # system-only transitions are never manual
    r = await client.post(f"/v1/cases/{c['id']}/transition", json={"to": "docs_pending"}, headers=h)
    assert r.status_code == 409
    # cancel (draft -> closed) is an officer manual transition; desk cannot
    c2 = await new_case(client, tok("desk1"), U())
    assert (
        await client.post(
            f"/v1/cases/{c2['id']}/transition", json={"to": "closed"}, headers=tok("desk1")
        )
    ).status_code == 409
    r = await client.post(
        f"/v1/cases/{c['id']}/transition", json={"to": "closed", "reason": "duplicate"}, headers=h
    )
    assert r.status_code == 200 and r.json()["to"] == "closed"
    tl = (await client.get(f"/v1/cases/{c['id']}/timeline", headers=h)).json()["events"]
    kinds = [(e["kind"], e.get("to") or e.get("type")) for e in tl]
    assert ("status", "closed") in kinds and ("audit", "case.created") in kinds
    assert (
        await client.post(f"/v1/cases/{c['id']}/transition", json={"to": "draft"}, headers=h)
    ).status_code == 409


async def test_unknown_and_malformed_ids(client: httpx.AsyncClient, tok: Any) -> None:
    for cid in (str(uuid.uuid4()), "not-a-uuid"):
        assert (await client.get(f"/v1/cases/{cid}", headers=tok("officer1"))).status_code == 404
        assert (
            await client.get(f"/v1/cases/{cid}/timeline", headers=tok("officer1"))
        ).status_code == 404
