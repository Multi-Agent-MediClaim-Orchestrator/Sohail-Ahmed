"""Endpoints the n8n flows call (doc 08 §4)."""

import uuid
from typing import Any

import httpx
import pytest
from tests.claims_helpers import ready_case
from tests.test_documents import sql

pytestmark = pytest.mark.integration


async def test_idempotency_endpoint(client: httpx.AsyncClient, tok: Any) -> None:
    k = {"workflow": "f1", "key": uuid.uuid4().hex}
    h = tok("svc:n8n")
    assert (await client.post("/v1/internal/idempotency", json=k, headers=h)).json() == {
        "duplicate": False
    }
    assert (await client.post("/v1/internal/idempotency", json=k, headers=h)).json() == {
        "duplicate": True
    }
    assert (
        await client.post("/v1/internal/idempotency", json=k, headers=tok("officer1"))
    ).status_code == 403
    bad = await client.post(
        "/v1/internal/idempotency", json={"workflow": "BAD NAME", "key": "x"}, headers=h
    )
    assert bad.status_code == 422


async def test_reminder_lifecycle_and_cap(
    cclient: httpx.AsyncClient, tok: Any, settings: Any, capp: Any
) -> None:
    case = await ready_case(cclient, tok)
    ids = []
    for i in range(2):
        rid = str(uuid.uuid4())
        ids.append(rid)
        sql(
            settings,
            "INSERT INTO reminder (id, case_id, kind, channel, fire_at, payload) VALUES (:i, :c, 'doc_request', 'in_app', now() - interval '1 minute' * :n, '{}'::jsonb)",
            i=rid,
            c=case["id"],
            n=i + 1,
        )
    h = tok("svc:n8n")
    due = (await cclient.get("/v1/internal/reminders/due?limit=200", headers=h)).json()["items"]
    assert {r["id"] for r in due} >= set(ids)
    assert (
        await cclient.post(
            f"/v1/internal/reminders/{ids[0]}/fired", json={"result": "in_app"}, headers=h
        )
    ).json()["status"] == "fired"
    assert (
        await cclient.post(f"/v1/internal/reminders/{ids[0]}/fired", json={}, headers=h)
    ).json()["status"] == "ignored"
    assert any(e.type == "reminder.due" for e in capp.state.hub.events)
    for _ in range(3):
        st = (
            await cclient.post(
                f"/v1/internal/reminders/{ids[1]}/failed", json={"error": "smtp down"}, headers=h
            )
        ).json()["status"]
    assert st == "failed"  # capped at 3 attempts
    assert ids[1] not in {
        r["id"]
        for r in (await cclient.get("/v1/internal/reminders/due?limit=200", headers=h)).json()[
            "items"
        ]
    }


async def test_notify_ops_and_reads(
    cclient: httpx.AsyncClient, tok: Any, capp: Any, settings: Any
) -> None:
    case = await ready_case(cclient, tok)
    h = tok("svc:n8n")
    r = await cclient.post(
        f"/v1/internal/cases/{case['id']}/notify", json={"event": "completeness.updated"}, headers=h
    )
    assert r.status_code == 202 and any(
        e.type == "completeness.updated" for e in capp.state.hub.events
    )
    assert (
        await cclient.post(
            f"/v1/internal/cases/{uuid.uuid4()}/notify", json={"event": "x.y"}, headers=h
        )
    ).status_code == 404
    assert (
        await cclient.post(
            f"/v1/internal/cases/{case['id']}/notify", json={"event": "BAD"}, headers=h
        )
    ).status_code == 422
    assert (
        await cclient.post(
            "/v1/internal/ops/events", json={"workflow": "w", "severity": "high"}, headers=h
        )
    ).status_code == 202
    ack = (await cclient.get(f"/v1/internal/cases/{case['id']}/ack-status", headers=h)).json()
    assert ack["acknowledged"] is False
    doc = (
        await cclient.get(f"/v1/internal/documents/{case['docs']['final_bill']}", headers=h)
    ).json()
    assert (
        doc["scan_status"] == "clean"
        and doc["case_id"] == case["id"]
        and doc["presigned_url"].startswith("http")
    )
    assert "full_name" not in doc
    assert (
        await cclient.get(f"/v1/internal/documents/{uuid.uuid4()}", headers=h)
    ).status_code == 404
    assert (
        await cclient.get("/v1/internal/cases/stale-building?older_than_s=0", headers=h)
    ).status_code == 200
    assert (await cclient.get("/v1/internal/outbox/stalled", headers=h)).status_code == 200
    assert (
        await cclient.get("/v1/internal/outbox/stalled", headers=tok("desk1"))
    ).status_code == 403


async def test_n8n_client_sends_the_webhook_secret() -> None:
    from app.services.n8n import HttpN8n

    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200)

    c = HttpN8n(
        "http://n8n/webhook", httpx.AsyncClient(transport=httpx.MockTransport(handler)), secret="s3"
    )
    assert await c.trigger("query/intake", {"query_id": "q"}, "k1")
    assert seen["x-webhook-secret"] == "s3" and seen["x-idempotency-key"] == "k1"


async def test_dashboard_summary(cclient: httpx.AsyncClient, tok: Any) -> None:
    await ready_case(cclient, tok)
    r = await cclient.get("/v1/dashboard/summary", headers=tok("officer1"))
    assert r.status_code == 200
    j = r.json()
    assert j["cases_by_status"].get("docs_complete", 0) >= 1 and {
        "queries_by_status",
        "overdue_queries",
        "overdue_requests",
        "deadlines",
    } <= set(j)
    assert (await cclient.get("/v1/dashboard/summary", headers=tok("hadmin"))).status_code == 403


async def test_hospital_registry_for_the_vision_service(
    client: httpx.AsyncClient, tok: Any
) -> None:
    r = await client.get("/v1/internal/hospitals", headers=tok("svc:internal"))
    assert r.status_code == 200 and r.json()["items"][0]["name"]
    assert (await client.get("/v1/internal/hospitals", headers=tok("desk1"))).status_code == 403


async def test_metrics_scrape_counts_routes_and_hides_ids(
    client: httpx.AsyncClient, tok: Any
) -> None:
    from app.core import metrics

    metrics.reset()
    case = await ready_case_light(client, tok)
    await client.get(f"/v1/cases/{case}", headers=tok("officer1"))
    r = await client.get("/v1/metrics", headers=tok("svc:internal"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'route="/v1/cases/{case_id}"' in body and case not in body  # template, never the raw id
    assert (
        "http_request_duration_seconds_bucket" in body
        and "outbox_rows" in body
        or "queries_open" in body
    )
    assert (await client.get("/v1/metrics", headers=tok("desk1"))).status_code == 403
    assert (await client.get("/v1/metrics", headers=tok("hadmin"))).status_code == 200


async def ready_case_light(c: httpx.AsyncClient, tok: Any) -> str:
    from tests.helpers import new_case

    return str((await new_case(c, tok("officer1"), "UH-" + uuid.uuid4().hex[:8]))["id"])
