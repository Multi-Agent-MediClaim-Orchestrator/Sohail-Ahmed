"""Audit explorer endpoints and lazily rendered page previews."""

import uuid
from typing import Any

import httpx
import pytest
from tests.helpers import files, new_case, pdf_bytes

pytestmark = pytest.mark.integration


async def test_audit_list_pagination_filter_and_roles(client: httpx.AsyncClient, tok: Any) -> None:
    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    for i in range(3):
        await client.post(
            f"/v1/cases/{case['id']}/documents",
            headers={**tok("desk1"), "Idempotency-Key": str(uuid.uuid4())},
            files=files((f"a{i}.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), "application/pdf")),
        )
    first = (await client.get(f"/v1/audit/{case['id']}?limit=4", headers=tok("officer1"))).json()
    assert [e["seq"] for e in first["items"]] == [1, 2, 3, 4] and first["next_after"] == 4
    rest = (
        await client.get(f"/v1/audit/{case['id']}?after=4&limit=200", headers=tok("officer1"))
    ).json()
    assert rest["items"][0]["seq"] == 5 and rest["next_after"] is None
    only = (
        await client.get(f"/v1/audit/{case['id']}?event_type=doc.uploaded", headers=tok("hadmin"))
    ).json()["items"]
    assert len(only) == 3 and all(e["event_type"] == "doc.uploaded" for e in only)
    assert all(len(e["hash"]) == 64 and e["prev_hash"] for e in first["items"])
    assert (await client.get(f"/v1/audit/{case['id']}", headers=tok("desk1"))).status_code == 403
    assert (
        await client.get(f"/v1/audit/{uuid.uuid4()}", headers=tok("officer1"))
    ).status_code == 404
    assert (await client.get("/v1/audit/not-a-uuid", headers=tok("officer1"))).status_code == 404


async def test_verify_reports_ok_then_the_exact_broken_sequence(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    await client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**tok("desk1"), "Idempotency-Key": str(uuid.uuid4())},
        files=files(("v.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), "application/pdf")),
    )
    ok = (await client.post(f"/v1/audit/{case['id']}/verify", headers=tok("officer1"))).json()
    assert ok["ok"] is True and ok["checked"] >= 3 and ok["broken_seq"] is None
    # tamper with event 2 as the table owner with triggers off (what an attacker with DB access would do)
    from sqlalchemy import text
    from tests.test_documents import sync_engine

    with sync_engine(settings, owner=True).begin() as c:
        c.execute(
            text("ALTER TABLE audit_event DISABLE TRIGGER USER")
        )  # the table is append-only for everyone else
        c.execute(
            text("UPDATE audit_event SET actor_id='mallory' WHERE case_id=:c AND seq=2"),
            {"c": case["id"]},
        )
        c.execute(text("ALTER TABLE audit_event ENABLE TRIGGER USER"))
    bad = (await client.post(f"/v1/audit/{case['id']}/verify", headers=tok("hadmin"))).json()
    assert bad["ok"] is False and bad["broken_seq"] == 2 and bad["reason"]
    assert (
        await client.post(f"/v1/audit/{case['id']}/verify", headers=tok("desk1"))
    ).status_code == 403


async def test_page_previews_render_once_and_reject_bad_pages(
    client: httpx.AsyncClient, tok: Any, app: Any
) -> None:
    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    r = await client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**tok("desk1"), "Idempotency-Key": str(uuid.uuid4())},
        files=files(("p.pdf", pdf_bytes(2, marker=uuid.uuid4().hex), "application/pdf")),
    )
    doc = r.json()["documents"][0]["id"]
    store = app.state.store
    key = f"{case['id']}/{doc}/pages/2.png"
    assert not await store.exists(key)
    a = await client.get(f"/v1/documents/{doc}/pages/2", headers=tok("officer1"))
    assert a.status_code == 302 and "pages/2.png" in a.headers["location"]
    assert await store.exists(key) and (await store.get(key))[:8] == b"\x89PNG\r\n\x1a\n"
    assert (
        await client.get(f"/v1/documents/{doc}/pages/2", headers=tok("officer1"))
    ).status_code == 302  # cached
    assert (
        await client.get(f"/v1/documents/{doc}/pages/3", headers=tok("officer1"))
    ).status_code == 404
    assert (
        await client.get(f"/v1/documents/{doc}/pages/0", headers=tok("officer1"))
    ).status_code == 404
