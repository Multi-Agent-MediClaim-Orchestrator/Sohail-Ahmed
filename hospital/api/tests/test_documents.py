"""Documents API (doc 03 §4.2-4.3, §8, §9) against real MinIO, ClamAV, Postgres and Redis."""

import asyncio
import hashlib
import io
import uuid
from typing import Any

import httpx
import pytest
from app.main import create_checked_app
from app.services import filechecks
from app.storage.clamav import ClamAV
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image
from sqlalchemy import create_engine, text
from tests.helpers import EICAR, encrypted_pdf, files, jpeg_bytes, js_pdf, new_case, pdf_bytes

pytestmark = pytest.mark.integration


def U() -> str:
    return "UH-" + uuid.uuid4().hex[:8]


def sync_engine(settings: Any, owner: bool = False) -> Any:
    url = settings.db_url.replace("+asyncpg", "+psycopg")
    return create_engine(
        url.replace("hosp_app", "hosp_owner").replace(_pw(url, "hosp_app"), _pw_owner())
        if owner
        else url
    )


def _pw(url: str, user: str) -> str:
    return url.split(f"{user}:")[1].split("@")[0]


def _pw_owner() -> str:
    from urllib.parse import quote

    from app.db.urls import _env

    return quote(_env()["HOSP_OWNER_PW"])


def force_status(settings: Any, case_id: str, status: str) -> None:
    eng = sync_engine(settings, owner=True)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE claim_case DISABLE TRIGGER trg_case_transition"))
        c.execute(
            text("UPDATE claim_case SET status=CAST(:s AS hospital_case_status) WHERE id=:i"),
            {"s": status, "i": case_id},
        )
        c.execute(text("ALTER TABLE claim_case ENABLE TRIGGER trg_case_transition"))


def sql(settings: Any, q: str, **p: Any) -> list[Any]:
    with sync_engine(settings, owner=True).begin() as c:
        r = c.execute(text(q), p)
        return list(r.all()) if r.returns_rows else []


async def upload(
    client: httpx.AsyncClient,
    h: dict[str, str],
    case_id: str,
    *items: tuple[str, bytes, str],
    data: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/v1/cases/{case_id}/documents", files=files(*items), data=data, headers=h
    )


PDF = "application/pdf"


async def test_upload_clean_pdf_full_effects(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    body = pdf_bytes(2, marker=uuid.uuid4().hex)
    before = len(app.state.n8n.calls)
    r = await upload(client, h, case["id"], ("bill.pdf", body, PDF))
    assert r.status_code == 202, r.text
    d = r.json()["documents"][0]
    assert (
        d["status"] == "accepted" and d["scan_status"] == "clean" and d["parse_status"] == "pending"
    )
    # n8n called exactly once with the document id as idempotency key
    new = app.state.n8n.calls[before:]
    assert len(new) == 1 and new[0][0] == "intake/document-uploaded" and new[0][2] == d["id"]
    assert new[0][1] == {"case_id": case["id"], "document_id": d["id"]}
    # case moved draft -> docs_pending; stored object exists with generated key and matching hash
    assert (await client.get(f"/v1/cases/{case['id']}", headers=h)).json()[
        "status"
    ] == "docs_pending"
    info = (await client.get(f"/v1/documents/{d['id']}", headers=h)).json()
    assert (
        info["pages"] == 2
        and info["sha256"] == hashlib.sha256(body).hexdigest()
        and info["usable"] is True
    )
    key = sql(settings, "SELECT storage_key FROM document WHERE id=:i", i=d["id"])[0][0]
    assert key == f"{case['id']}/{d['id']}/original.pdf" and await app.state.store.exists(key)
    # SSE order and audit
    evs = [e.type for e in app.state.hub.events if e.data.get("document_id") == d["id"]]
    assert evs == ["doc.uploaded", "doc.scanned"]
    types = [
        r[0]
        for r in sql(
            settings,
            "SELECT event_type FROM audit_event WHERE case_id=:c ORDER BY seq",
            c=case["id"],
        )
    ]
    assert types[:4] == ["case.created", "doc.uploaded", "doc.scanned", "case.status_changed"] or {
        "doc.uploaded",
        "doc.scanned",
    } <= set(types)
    pii = sql(settings, "SELECT payload::text FROM audit_event WHERE case_id=:c", c=case["id"])
    assert all("bill.pdf" not in x[0] for x in pii)  # no filenames/PII in audit payloads
    listing = (await client.get(f"/v1/cases/{case['id']}/documents", headers=h)).json()["documents"]
    assert [x["id"] for x in listing] == [d["id"]]


async def test_eicar_quarantined_and_unreachable(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    r = await upload(client, h, case["id"], ("eicar.pdf", EICAR, PDF))
    assert r.status_code == 422
    res = r.json()["documents"][0]
    assert (
        res["status"] == "rejected"
        and res["error"]["code"] == "infected"
        and "Eicar" in res["error"]["signature"]
    )
    row = sql(
        settings,
        "SELECT id, lifecycle, scan_status, storage_key FROM document WHERE case_id=:c",
        c=case["id"],
    )[0]
    assert (row[1], row[2]) == ("quarantined", "infected") and row[3].startswith("quarantine/")
    keys = await app.state.store.list_keys(prefix=case["id"])
    assert keys == []  # nothing stored under the normal prefix
    assert (await client.get(f"/v1/documents/{row[0]}/download", headers=h)).status_code == 404
    assert (await client.get(f"/v1/cases/{case['id']}/documents", headers=h)).json()[
        "documents"
    ] == []
    again = await upload(client, h, case["id"], ("eicar.pdf", EICAR, PDF))
    assert again.status_code == 422


@pytest.mark.parametrize(
    ("name", "content", "mime", "code"),
    [
        ("virus.pdf", b"MZ\x90\x00\x03" + b"\x00" * 100, PDF, "unsupported_media_type"),
        ("photo.jpg", b"just some text, not an image", "image/jpeg", "unsupported_media_type"),
        ("empty.pdf", b"", PDF, "empty_file"),
        (
            "renamed.png",
            pdf_bytes(1, marker="x"),
            "image/png",
            "unsupported_media_type",
        ),  # PDF with .png extension
        ("broken.pdf", b"%PDF-1.4\nthis is not a real pdf", PDF, "corrupt_file"),
        (
            "script.pdf",
            js_pdf(),
            PDF,
            "infected|active_content",
        ),  # ClamAV heuristics may catch it first
        ("locked.pdf", encrypted_pdf(), PDF, "encrypted_pdf"),
    ],
)
async def test_rejected_files(
    client: httpx.AsyncClient, tok: Any, name: str, content: bytes, mime: str, code: str
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    r = await upload(client, h, case["id"], (name, content, mime))
    assert r.status_code == 422, r.text
    assert r.json()["documents"][0]["error"]["code"] in code.split("|")


async def test_oversize_and_image_bomb(client: httpx.AsyncClient, tok: Any) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    big = b"%PDF-1.4\n" + b"0" * (26 * 1024 * 1024)
    r = await upload(client, h, case["id"], ("big.pdf", big, PDF))
    assert r.status_code == 422 and r.json()["documents"][0]["error"]["code"] == "file_too_large"
    bomb = io.BytesIO()
    Image.new("1", (15000, 15000)).save(bomb, format="PNG")
    r = await upload(client, h, case["id"], ("bomb.png", bomb.getvalue(), "image/png"))
    assert r.json()["documents"][0]["error"]["code"] in ("image_too_large", "corrupt_file")


async def test_mixed_batch_partial_success(client: httpx.AsyncClient, tok: Any) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    ok = pdf_bytes(1, marker=uuid.uuid4().hex)
    r = await upload(
        client,
        h,
        case["id"],
        ("ok.pdf", ok, PDF),
        ("bad.exe", b"MZ" + b"\0" * 50, PDF),
        ("ok2.pdf", ok, PDF),
    )
    assert r.status_code == 202
    s = [d["status"] for d in r.json()["documents"]]
    assert s == ["accepted", "rejected", "skipped_duplicate"]


async def test_duplicate_sequential_and_concurrent(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    body = pdf_bytes(1, marker=uuid.uuid4().hex)
    first = (await upload(client, h, case["id"], ("a.pdf", body, PDF))).json()["documents"][0]
    dup = (await upload(client, h, case["id"], ("b.pdf", body, PDF))).json()["documents"][0]
    assert dup["status"] == "skipped_duplicate" and dup["duplicate_of"] == first["id"]
    other = await new_case(client, h, U())  # same bytes in another case is allowed
    assert (await upload(client, h, other["id"], ("a.pdf", body, PDF))).json()["documents"][0][
        "status"
    ] == "accepted"
    fresh = pdf_bytes(1, marker=uuid.uuid4().hex)
    rs = await asyncio.gather(
        *[upload(client, h, case["id"], ("c.pdf", fresh, PDF)) for _ in range(20)]
    )
    statuses = sorted(r.json()["documents"][0]["status"] for r in rs)
    assert statuses.count("accepted") == 1 and statuses.count("skipped_duplicate") == 19
    n = sql(
        settings,
        "SELECT count(*) FROM document WHERE case_id=:c AND sha256=:s",
        c=case["id"],
        s=hashlib.sha256(fresh).hexdigest(),
    )[0][0]
    assert n == 1


async def test_gps_exif_stripped(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    raw = jpeg_bytes(gps=True, color=(uuid.uuid4().int % 255, 5, 5))
    r = await upload(client, h, case["id"], ("scan.jpg", raw, "image/jpeg"))
    d = r.json()["documents"][0]
    key, sha = sql(settings, "SELECT storage_key, sha256 FROM document WHERE id=:i", i=d["id"])[0]
    stored = await app.state.store.get(key)
    assert 0x8825 not in Image.open(io.BytesIO(stored)).getexif()
    assert sha == hashlib.sha256(stored).hexdigest()  # recorded hash is that of the stored bytes
    clean = jpeg_bytes(size=70)
    r = await upload(client, h, case["id"], ("clean.jpg", clean, "image/jpeg"))
    key2 = sql(
        settings, "SELECT storage_key FROM document WHERE id=:i", i=r.json()["documents"][0]["id"]
    )[0][0]
    assert await app.state.store.get(key2) == clean  # no GPS: bytes untouched


async def test_supersede(client: httpx.AsyncClient, tok: Any, app: Any) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    old = (
        await upload(client, h, case["id"], ("v1.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF))
    ).json()["documents"][0]
    before = len(app.state.completeness_calls)
    new = (
        await upload(
            client,
            h,
            case["id"],
            ("v2.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF),
            data={"supersedes_id": old["id"]},
        )
    ).json()["documents"][0]
    assert new["status"] == "accepted"
    active = [
        d["id"]
        for d in (await client.get(f"/v1/cases/{case['id']}/documents", headers=h)).json()[
            "documents"
        ]
    ]
    assert active == [new["id"]]
    allv = (await client.get(f"/v1/cases/{case['id']}/documents?lifecycle=all", headers=h)).json()[
        "documents"
    ]
    assert {d["id"]: d["lifecycle"] for d in allv} == {old["id"]: "superseded", new["id"]: "active"}
    assert len(app.state.completeness_calls) > before  # completeness re-run triggered
    for bad in (old["id"], str(uuid.uuid4()), "nope"):  # already superseded / unknown / malformed
        r = await upload(
            client,
            h,
            case["id"],
            ("v3.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF),
            data={"supersedes_id": bad},
        )
        assert r.json()["documents"][0]["error"]["code"] == "invalid_supersede"
    other = await new_case(client, h, U())
    r = await upload(
        client,
        h,
        other["id"],
        ("x.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF),
        data={"supersedes_id": new["id"]},
    )
    assert r.json()["documents"][0]["error"]["code"] == "invalid_supersede"  # other case's document


async def test_delete_tombstone_reupload_and_locks(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    body = pdf_bytes(1, marker=uuid.uuid4().hex)
    d = (await upload(client, h, case["id"], ("a.pdf", body, PDF))).json()["documents"][0]
    key = sql(settings, "SELECT storage_key FROM document WHERE id=:i", i=d["id"])[0][0]
    assert (await client.delete(f"/v1/documents/{d['id']}", headers=h)).status_code == 204
    row = sql(
        settings, "SELECT lifecycle, storage_key, purge_after FROM document WHERE id=:i", i=d["id"]
    )[0]
    assert row[0] == "deleted" and row[1].startswith("tombstone/") and row[2] is not None
    assert not await app.state.store.exists(key) and await app.state.store.exists(row[1])
    assert (await client.get(f"/v1/documents/{d['id']}/download", headers=h)).status_code == 410
    assert (
        await client.delete(f"/v1/documents/{d['id']}", headers=h)
    ).status_code == 409  # already deleted
    again = (await upload(client, h, case["id"], ("a.pdf", body, PDF))).json()["documents"][0]
    assert (
        again["status"] == "accepted" and again["id"] != d["id"]
    )  # re-upload after delete is a new document
    # building_claim / submitted lock the case
    force_status(settings, case["id"], "building_claim")
    r = await upload(client, h, case["id"], ("n.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF))
    assert r.status_code == 423 and r.json()["code"] == "case_locked"
    assert (await client.delete(f"/v1/documents/{again['id']}", headers=h)).status_code == 409
    force_status(settings, case["id"], "submitted")
    assert (
        await upload(client, h, case["id"], ("n.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF))
    ).status_code == 423
    r = await client.delete(f"/v1/documents/{again['id']}", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "case_submitted"
    assert (
        await client.patch(
            f"/v1/documents/{again['id']}", json={"doc_type": "lab_report"}, headers=h
        )
    ).status_code == 409


async def test_ready_for_review_upload_returns_to_docs_pending(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    force_status(settings, case["id"], "ready_for_review")
    r = await upload(client, h, case["id"], ("n.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF))
    assert r.status_code == 202
    assert (await client.get(f"/v1/cases/{case['id']}", headers=h)).json()[
        "status"
    ] == "docs_pending"


async def test_download_and_scope(client: httpx.AsyncClient, tok: Any) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    body = pdf_bytes(1, marker=uuid.uuid4().hex)
    d = (await upload(client, h, case["id"], ("a b.pdf", body, PDF))).json()["documents"][0]
    r = await client.get(f"/v1/documents/{d['id']}/download", headers=h, follow_redirects=False)
    assert r.status_code == 302
    assert "attachment" in r.headers["location"] and "X-Amz-Signature" in r.headers["location"]
    async with httpx.AsyncClient() as ext:
        assert (await ext.get(r.headers["location"])).content == body
    officer = (await client.get("/v1/me", headers=tok("officer2"))).json()["id"]
    await client.post(
        f"/v1/cases/{case['id']}/assign", json={"user_id": officer}, headers=tok("officer1")
    )
    assert (
        await client.get(f"/v1/documents/{d['id']}", headers=h)
    ).status_code == 404  # desk1 lost scope
    assert (await client.get(f"/v1/documents/{d['id']}/download", headers=h)).status_code == 404
    assert (
        await client.get(f"/v1/documents/{d['id']}", headers=tok("officer1"))
    ).status_code == 200
    assert (
        await client.get(f"/v1/documents/{d['id']}/pages/1", headers=tok("officer1"))
    ).status_code == 404  # no preview yet


async def test_reclassify_manual_wins_and_reparse(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    d = (
        await upload(
            client,
            h,
            case["id"],
            ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF),
            data={"doc_type_hint": "lab_report"},
        )
    ).json()["documents"][0]
    info = (await client.get(f"/v1/documents/{d['id']}", headers=h)).json()
    assert info["doc_type"] == "lab_report" and info["doc_type_source"] == "manual"
    before = len(app.state.completeness_calls)
    r = await client.patch(f"/v1/documents/{d['id']}", json={"doc_type": "final_bill"}, headers=h)
    assert (
        r.status_code == 200
        and r.json()["doc_type"] == "final_bill"
        and r.json()["doc_type_source"] == "manual"
    )
    assert len(app.state.completeness_calls) > before
    assert (
        await client.patch(f"/v1/documents/{d['id']}", json={"doc_type": "bogus"}, headers=h)
    ).status_code == 422
    # auto classification can never overwrite a manual choice
    svc = tok("svc:crew")
    r = await client.post(
        f"/v1/internal/documents/{d['id']}/classify",
        json={"doc_type": "prescription", "confidence": 0.99},
        headers=svc,
    )
    assert r.status_code == 200 and r.json()["ignored"] == "manual_classification_wins"
    assert (await client.get(f"/v1/documents/{d['id']}", headers=h)).json()[
        "doc_type"
    ] == "final_bill"
    # reparse
    r = await client.post(f"/v1/documents/{d['id']}/reparse", headers=h)
    assert r.status_code == 202 and r.json()["parse_status"] == "pending"
    sql(settings, "UPDATE document SET parse_status='processing' WHERE id=:i", i=d["id"])
    assert (await client.post(f"/v1/documents/{d['id']}/reparse", headers=h)).status_code == 409


async def make_doc(client: httpx.AsyncClient, tok: Any, hint: str | None = None) -> tuple[str, str]:
    h = tok("desk1")
    case = await new_case(client, h, U())
    r = await upload(
        client,
        h,
        case["id"],
        ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF),
        data={"doc_type_hint": hint} if hint else None,
    )
    return case["id"], r.json()["documents"][0]["id"]


async def test_parse_callbacks_two_pass_agreement(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    case_id, doc = await make_doc(client, tok)
    svc = tok("svc:n8n")
    crew = tok("svc:crew")
    r = await client.post(
        f"/v1/internal/documents/{doc}/classify",
        json={"doc_type": "itemised_bill", "confidence": 0.93},
        headers=crew,
    )
    assert r.status_code == 200 and r.json()["accepted"] is True
    r = await client.post(
        f"/v1/internal/documents/{doc}/quality",
        json={"quality_score": 0.82, "flags": ["skewed"], "has_required_stamp": True},
        headers=svc,
    )
    assert r.status_code == 200
    p1 = {
        "pass_no": 1,
        "engine": "mineru-pipeline",
        "engine_version": "2.1.0",
        "confidence": 0.88,
        "typed_json": {
            "total": "148230.00",
            "date": "2026-10-01",
            "lines": [{"description": "Room", "amount": "16000.00"}],
        },
        "masked_text_key": f"{case_id}/{doc}/masked.txt",
        "duration_ms": 8120,
    }
    r = await client.post(f"/v1/internal/documents/{doc}/parse", json=p1, headers=svc)
    assert r.json() == {"document_id": doc, "parse_status": "processing", "next": "pass_2_expected"}
    # replay: identical payload -> single effect, single audit event
    await client.post(f"/v1/internal/documents/{doc}/parse", json=p1, headers=svc)
    n_audit = sql(
        settings,
        "SELECT count(*) FROM audit_event WHERE case_id=:c AND event_type='doc.parsed'",
        c=case_id,
    )[0][0]
    assert n_audit == 1
    p2 = {
        **p1,
        "pass_no": 2,
        "engine": "crew-llm",
        "confidence": 0.9,
        "typed_json": {"total": "Rs. 1,48,230/-", "date": "2026-10-01"},
    }
    r = await client.post(f"/v1/internal/documents/{doc}/parse", json=p2, headers=crew)
    assert (
        r.status_code == 200
        and r.json()["parse_status"] == "parsed"
        and r.json()["agreement_score"] == 1.0
    )
    detail = (await client.get(f"/v1/documents/{doc}/parse", headers=tok("desk1"))).json()
    assert [x["pass_no"] for x in detail["passes"]] == [1, 2] and detail["agreement_score"] == 1.0
    assert "raw_markdown_key" not in str(detail) and detail["fields_disagreeing"] == []
    info = (await client.get(f"/v1/documents/{doc}", headers=tok("desk1"))).json()
    assert (
        info["parse_status"] == "parsed"
        and info["quality"]["flags"] == ["skewed"]
        and info["quality"]["has_required_stamp"]
    )
    # disagreement on the amount -> needs_review
    p2b = {**p2, "typed_json": {"total": "148000.00", "date": "2026-10-01"}}
    r = await client.post(f"/v1/internal/documents/{doc}/parse", json=p2b, headers=crew)
    assert r.json()["parse_status"] == "needs_review" and r.json()["agreement_score"] == 0.5
    detail = (await client.get(f"/v1/documents/{doc}/parse", headers=tok("desk1"))).json()
    assert detail["fields_disagreeing"] == ["total"]


async def test_classification_gate_below_threshold(client: httpx.AsyncClient, tok: Any) -> None:
    _, doc = await make_doc(client, tok)
    r = await client.post(
        f"/v1/internal/documents/{doc}/classify",
        json={"doc_type": "final_bill", "confidence": 0.5},
        headers=tok("svc:crew"),
    )
    assert r.json()["accepted"] is False and r.json()["parse_status"] == "needs_review"
    info = (await client.get(f"/v1/documents/{doc}", headers=tok("desk1"))).json()
    assert info["doc_type"] is None and info["needs_attention"] is True


async def test_callback_authz_validation_and_inactive(client: httpx.AsyncClient, tok: Any) -> None:
    _, doc = await make_doc(client, tok)
    body = {"quality_score": 0.5, "flags": []}
    for who, code in (("desk1", 403), ("officer1", 403), ("hadmin", 403)):
        assert (
            await client.post(f"/v1/internal/documents/{doc}/quality", json=body, headers=tok(who))
        ).status_code == code
    assert (
        await client.post(f"/v1/internal/documents/{doc}/quality", json=body)
    ).status_code == 401
    r = await client.get("/v1/internal/documents/pending-parse", headers=tok("svc:crew"))
    assert (
        r.status_code == 403 and r.json()["code"] == "wrong_service"
    )  # crew is not allowed on n8n-only routes
    assert (
        await client.get("/v1/internal/documents/pending-parse", headers=tok("svc:n8n"))
    ).status_code == 200
    r = await client.post(
        f"/v1/internal/documents/{doc}/quality",
        json={"quality_score": 2, "flags": []},
        headers=tok("svc:n8n"),
    )
    assert r.status_code == 422
    r = await client.post(
        f"/v1/internal/documents/{doc}/quality", json={**body, "extra": 1}, headers=tok("svc:n8n")
    )
    assert r.status_code == 422
    assert (
        await client.post(
            f"/v1/internal/documents/{uuid.uuid4()}/quality", json=body, headers=tok("svc:n8n")
        )
    ).status_code == 404
    await client.delete(f"/v1/documents/{doc}", headers=tok("desk1"))
    r = await client.post(
        f"/v1/internal/documents/{doc}/quality", json=body, headers=tok("svc:n8n")
    )
    assert r.status_code == 409 and r.json()["code"] == "document_not_active"


async def test_split_mixed_pdf(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    doc = (
        await upload(
            client, h, case["id"], ("mixed.pdf", pdf_bytes(5, marker=uuid.uuid4().hex), PDF)
        )
    ).json()["documents"][0]
    svc = tok("svc:n8n")
    bad = await client.post(
        f"/v1/internal/documents/{doc['id']}/split",
        headers=svc,
        json={"children": [{"page_from": 1, "page_to": 3}, {"page_from": 3, "page_to": 5}]},
    )
    assert bad.status_code == 422  # overlapping ranges
    bad = await client.post(
        f"/v1/internal/documents/{doc['id']}/split",
        headers=svc,
        json={"children": [{"page_from": 1, "page_to": 2}, {"page_from": 3, "page_to": 9}]},
    )
    assert bad.status_code == 422  # beyond the page count
    before = len(app.state.n8n.calls)
    r = await client.post(
        f"/v1/internal/documents/{doc['id']}/split",
        headers=svc,
        json={
            "children": [
                {"page_from": 1, "page_to": 2, "doc_type_hint": "final_bill"},
                {"page_from": 3, "page_to": 5},
            ]
        },
    )
    assert r.status_code == 200 and len(r.json()["children"]) == 2
    rows = sql(
        settings,
        "SELECT pages, parent_id::text, lower(page_range), upper(page_range) FROM document "
        "WHERE parent_id=:p ORDER BY lower(page_range)",
        p=doc["id"],
    )
    assert [(x[0], x[1], x[2], x[3]) for x in rows] == [(2, doc["id"], 1, 3), (3, doc["id"], 3, 6)]
    assert (
        sql(settings, "SELECT lifecycle FROM document WHERE id=:i", i=doc["id"])[0][0]
        == "superseded"
    )
    assert len(app.state.n8n.calls) - before == 2  # each child triggers parsing
    child = r.json()["children"][0]
    assert (await client.get(f"/v1/documents/{child}", headers=h)).json()["parent_id"] == doc["id"]


async def test_sweeper_retriggers_then_fails(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    case_id, doc = await make_doc(client, tok)
    svc = tok("svc:n8n")
    before = len(app.state.n8n.calls)
    await client.post("/v1/internal/jobs/sweeper", headers=svc)
    assert not any(
        c[1].get("document_id") == doc for c in app.state.n8n.calls[before:]
    )  # not old enough yet
    for expected in range(
        1, 5
    ):  # parse_attempts counts re-triggers; the upload trigger is attempt 0 (5 total)
        sql(
            settings,
            "UPDATE document SET last_trigger_at = now() - interval '5 minutes' WHERE id=:i",
            i=doc,
        )
        before = len(app.state.n8n.calls)
        r = await client.post("/v1/internal/jobs/sweeper", headers=svc)
        assert r.json()["retriggered"] >= 1
        assert any(c[1].get("document_id") == doc for c in app.state.n8n.calls[before:])
        assert (
            sql(settings, "SELECT parse_attempts FROM document WHERE id=:i", i=doc)[0][0]
            == expected
        )
    sql(
        settings,
        "UPDATE document SET last_trigger_at = now() - interval '5 minutes' WHERE id=:i",
        i=doc,
    )
    r = await client.post("/v1/internal/jobs/sweeper", headers=svc)  # 5th attempt -> failed
    assert r.json()["failed"] >= 1
    assert sql(settings, "SELECT parse_status FROM document WHERE id=:i", i=doc)[0][0] == "failed"
    pend = (
        await client.get("/v1/internal/documents/pending-parse?older_than_s=0", headers=svc)
    ).json()["documents"]
    assert doc not in [d["id"] for d in pend]


async def test_n8n_down_document_stays_pending(settings: Any, tok: Any) -> None:
    from app.services.n8n import RecordingN8n

    app = create_checked_app(settings, n8n=RecordingN8n(fail=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://h"
        ) as c:
            case = await new_case(c, tok("desk1"), U())
            r = await upload(
                c, tok("desk1"), case["id"], ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)
            )
            assert r.status_code == 202
            d = (
                await c.get(f"/v1/documents/{r.json()['documents'][0]['id']}", headers=tok("desk1"))
            ).json()
            assert d["parse_status"] == "pending"


async def test_scanner_down_fails_closed(settings: Any, tok: Any) -> None:
    app = create_checked_app(settings, clam=ClamAV("127.0.0.1", 1, timeout=2))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://h"
        ) as c:
            case = await new_case(c, tok("desk1"), U())
            r = await upload(
                c, tok("desk1"), case["id"], ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)
            )
            assert r.status_code == 503 and r.json()["code"] == "scan_unavailable"
            assert (
                sql(settings, "SELECT count(*) FROM document WHERE case_id=:c", c=case["id"])[0][0]
                == 0
            )


async def test_upload_rate_limit(settings: Any, tok: Any) -> None:
    app = create_checked_app(settings.model_copy(update={"upload_rate_per_min": 3}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://h"
        ) as c:
            h = {**tok("officer2")}
            case = await new_case(c, h, U())
            await app.state.redis.delete(
                *[k async for k in app.state.redis.scan_iter("rl:hosp:upload:*")] or ["x"]
            )
            codes = []
            for _ in range(5):
                r = await upload(
                    c, h, case["id"], ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)
                )
                codes.append(r.status_code)
            assert codes[:3] == [202, 202, 202] and codes[3:] == [429, 429]
            assert r.headers["retry-after"] and r.json()["code"] == "rate_limited"


async def test_tombstone_purge_stale_and_orphans(
    client: httpx.AsyncClient, tok: Any, app: Any, settings: Any
) -> None:
    h = tok("desk1")
    case = await new_case(client, h, U())
    d = (
        await upload(client, h, case["id"], ("a.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF))
    ).json()["documents"][0]
    await client.delete(f"/v1/documents/{d['id']}", headers=h)
    tomb = sql(settings, "SELECT storage_key FROM document WHERE id=:i", i=d["id"])[0][0]
    sql(
        settings,
        "UPDATE document SET purge_after = now() - interval '1 day' WHERE id=:i",
        i=d["id"],
    )
    svc = tok("svc:n8n")
    r = await client.post("/v1/internal/jobs/purge-tombstones", headers=svc)
    assert r.json()["purged"] >= 1
    assert not await app.state.store.exists(tomb)
    row = sql(settings, "SELECT storage_key, lifecycle FROM document WHERE id=:i", i=d["id"])[0]
    assert row[0] is None and row[1] == "deleted"  # row kept for audit
    assert (await client.get(f"/v1/documents/{d['id']}/download", headers=h)).status_code == 410
    old_draft = await new_case(
        client, h, U()
    )  # the first case left draft when its document was uploaded
    sql(
        settings,
        "UPDATE claim_case SET created_at = now() - interval '40 days' WHERE id=:i",
        i=old_draft["id"],
    )
    c2 = await new_case(client, h, U())
    r = await client.post("/v1/internal/jobs/stale-drafts", headers=svc)
    assert r.json()["flagged"] >= 1
    flagged = "SELECT stale_flagged_at IS NOT NULL FROM claim_case WHERE id=:i"
    assert sql(settings, flagged, i=old_draft["id"])[0][0] is True
    assert sql(settings, flagged, i=c2["id"])[0][0] is False
    orphan = f"00000000-{uuid.uuid4().hex[:8]}/{uuid.uuid4()}/original.pdf"  # sorts first: the report is capped at 200 keys
    await app.state.store.put(orphan, b"x")
    r = await client.post("/v1/internal/jobs/orphan-scan", headers=svc)
    assert orphan in r.json()["orphans"]
    await app.state.store.delete(orphan)


async def test_endpoints_in_openapi_with_operation_ids(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert {
        "createCase",
        "listCases",
        "getCase",
        "patchCase",
        "assignCase",
        "transitionCase",
        "caseTimeline",
        "uploadDocuments",
        "listDocuments",
        "getDocument",
        "downloadDocument",
        "documentPage",
        "getParse",
        "patchDocument",
        "deleteDocument",
        "reparseDocument",
        "cbQuality",
        "cbParse",
        "cbClassify",
        "cbStatus",
        "cbSplit",
        "pendingParse",
        "getMe",
        "listUsers",
        "patchUser",
        "health",
        "ready",
    } <= ops


# ---- pure unit tests -------------------------------------------------------------------------
@given(st.text(max_size=1500))
def test_filename_sanitizer_fuzz(name: str) -> None:
    out = filechecks.sanitize_filename(name)
    assert "/" not in out and "\\" not in out and ".." not in out and "\x00" not in out
    assert not any(c in out for c in "‮‭⁦") and 0 < len(out) <= 120


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        (b"%PDF-1.7", "application/pdf"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"II*\x00....", "image/tiff"),
        (b"MM\x00*....", "image/tiff"),
        (b"MZ\x90", None),
        (b"", None),
        (b"hello", None),
    ],
)
def test_sniff_table(head: bytes, mime: str | None) -> None:
    assert filechecks.sniff(head) == mime


def test_pdf_checks_unit() -> None:
    from app.core.errors import ApiError

    assert filechecks.check_pdf(pdf_bytes(3), 200) == 3
    for data, code in (
        (js_pdf(), "active_content"),
        (encrypted_pdf(), "encrypted_pdf"),
        (b"%PDF-1.4 junk", "corrupt_file"),
    ):
        with pytest.raises(ApiError) as ei:
            filechecks.check_pdf(data, 200)
        assert ei.value.code == code
    with pytest.raises(ApiError) as ei:
        filechecks.check_pdf(pdf_bytes(5), 4)  # too many pages
    assert ei.value.code == "file_too_large"


def test_ext_matches() -> None:
    assert filechecks.ext_matches("application/pdf", "a.PDF") and not filechecks.ext_matches(
        "application/pdf", "a.png"
    )
    assert filechecks.ext_matches("image/jpeg", "x.jpeg") and not filechecks.ext_matches(
        "image/jpeg", "noext"
    )
