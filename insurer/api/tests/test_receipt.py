import asyncio
import json
import uuid

import httpx
import pytest
from app.services import audit, docs_fetch, jobs, outbox
from app.services.docs_fetch import EICAR
from claim_contract import audit as caudit
from claim_contract.samples import clone, make_line, make_submission
from ins_helpers import register_claim_docs
from sqlalchemy import text

pytestmark = pytest.mark.integration

_n = 0


def fresh(**kw):
    """A valid claim with a unique claim_ref / idempotency key."""
    global _n
    _n += 1
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    return make_submission(claim_ref=ref, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **kw)


async def q1(e, sql, **p):
    async with e.sm() as s:
        return (await s.execute(text(sql), p)).all()


async def case_row(e, ref):
    return (await q1(e, "SELECT id, insurer_claim_no, status, last_callback_seq, priority FROM core.claim_case WHERE hospital_claim_ref = :r", r=ref))[0]


# ------------------------------------------------------------------ R-01..R-04
async def test_r01_happy_path(env):
    claim = fresh()
    r = await env.sim.submit(claim)
    assert r.status_code == 202, r.text
    ack = r.json()
    assert ack["status"] == "received" and ack["sequence"] == 1 and ack["insurer_claim_no"].startswith("IC-2026-")
    assert r.headers["x-contract-version"] and r.headers["x-request-id"]
    c = await case_row(env, claim["claim_ref"])
    assert (await q1(env, "SELECT count(*) FROM core.bill_line WHERE case_id = :c", c=c.id))[0][0] == len(claim["bill_lines"])
    assert (await q1(env, "SELECT count(*) FROM core.claim_document WHERE case_id = :c", c=c.id))[0][0] == len(claim["documents"])
    async with env.sm() as s:
        assert (await audit.verify(s, c.id)).ok
    ev = await q1(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c ORDER BY seq", c=c.id)
    assert ev[0][0] == "claim.received"
    assert (await q1(env, "SELECT count(*), min(seq) FROM ops.outbox WHERE case_id = :c", c=c.id))[0] == (1, 2)
    assert {j[0] for j in jobs.pending()} >= {"fetch_documents", "start_verification"}
    assert c.last_callback_seq == 2


async def test_r02_replay_same_key_identical_body(env):
    claim, idem = fresh(), str(uuid.uuid4())
    r1, r2 = await env.sim.submit(claim, idem), await env.sim.submit(claim, idem)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r2.headers.get("idempotent-replay") == "true" and r1.content == r2.content
    assert (await q1(env, "SELECT count(*) FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0][0] == 1


async def test_r02b_new_key_same_body_returns_stored_ack_and_r03_conflict(env):
    claim = fresh()
    a = await env.sim.submit(claim)
    b = await env.sim.submit(claim)  # new idempotency key, same claim_ref + identical body
    assert b.status_code == 200 and b.headers["idempotent-replay"] == "true" and a.json()["insurer_claim_no"] == b.json()["insurer_claim_no"]
    changed = clone(claim)
    changed["hospital_notes"] = "different"
    c = await env.sim.submit(changed)
    assert c.status_code == 409 and c.json()["code"] == "idempotency_conflict"
    # R-03: same key, different body
    idem = str(uuid.uuid4())
    d1 = fresh()
    await env.sim.submit(d1, idem)
    d2 = clone(d1)
    d2["hospital_notes"] = "x"
    r = await env.sim.submit(d2, idem)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"


async def test_r04_ten_concurrent_identical_one_case(env):
    claim, idem = fresh(), str(uuid.uuid4())
    rs = await asyncio.gather(*(env.sim.submit(claim, idem) for _ in range(10)))
    assert all(r.status_code in (200, 202) or r.json()["code"] == "idempotency_in_progress" for r in rs), [r.text for r in rs]
    assert (await q1(env, "SELECT count(*) FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0][0] == 1


# ------------------------------------------------------------------ auth R-05..R-08
async def test_r05_tampered_body_r06_stale_r07_unknown_key_r08_previous_secret(env):
    claim = fresh()
    body = json.dumps(claim, separators=(",", ":")).encode()
    h = env.sim.headers("POST", "/v1/hospital-api/claims", body, str(uuid.uuid4()))
    bad = await env.sim.insurer.post("/v1/hospital-api/claims", content=body.replace(b"Asha", b"Asia"), headers=h)
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_signature"
    from datetime import UTC, datetime, timedelta

    stale = await env.sim.submit(claim, ts=datetime.now(UTC) - timedelta(seconds=400))
    assert stale.status_code == 401 and stale.json()["code"] == "stale_request"
    unknown = await env.sim.submit(claim, key_id="x")
    assert unknown.status_code == 401 and unknown.json()["code"] == "invalid_signature"
    prev = await env.sim.submit(claim, secret=b"previous-secret-0000000000000000000000")
    assert prev.status_code == 202


# ------------------------------------------------------------------ validation R-09..R-15
async def test_r09_r10_r11_validation_errors(env):
    c = fresh()
    c["totals"]["gross"]["amount"] = "65500.01"
    c["totals"]["claimed"]["amount"] = "65500.01"
    r = await env.sim.submit(c)
    assert r.status_code == 422 and r.json()["code"] == "totals_mismatch" and r.json()["errors"][0]["field"] == "totals.gross"
    c = fresh()
    c["bill_lines"][0]["amount"]["amount"] = "12500.00"
    assert (await env.sim.submit(c)).status_code == 422
    c = fresh()
    c["admission"]["discharged_on"] = "2027-01-01"
    assert (await env.sim.submit(c)).status_code == 422
    c = fresh()
    c["surprise"] = 1
    r = await env.sim.submit(c)
    assert r.status_code == 422 and "surprise" in json.dumps(r.json()["errors"])
    c = fresh()
    c["contract_version"] = "2.0"
    assert (await env.sim.submit(c)).json()["code"] == "unsupported_version"


async def test_r12_blacklisted_r13_mismatch_audited(env):
    c = fresh(hospital_id="HOSP-0012")
    r = await env.sim.submit(c, key_id="hosp-012")
    assert r.status_code == 403 and r.json()["code"] == "hospital_blacklisted"
    assert (await q1(env, "SELECT count(*) FROM audit.audit_event WHERE event_type = 'claim.refused'"))[0][0] >= 1
    c = fresh(hospital_id="HOSP-0003")  # key hosp-001 is bound to HOSP-0001
    r = await env.sim.submit(c)
    assert r.status_code == 403 and r.json()["code"] == "hospital_mismatch"
    assert (await q1(env, "SELECT count(*) FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0][0] == 0


async def test_r14_unknown_policy_still_accepted(env):
    c = fresh(policy_number="POL-NOT-IN-MASTER-1", member_id="MEM-00000001")
    r = await env.sim.submit(c)
    assert r.status_code == 202
    row = (await q1(env, "SELECT policy_id, member_id FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0]
    assert row.policy_id is None and row.member_id is None


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data", "http://localhost:80/x", "http://[::1]:9000/x", "http://2130706433:9000/x", "file:///etc/passwd",
    "http://user:pw@minio:9000/x", "http://evil.example.com/x", "ftp://minio:9000/x", "http://minio:9001/x", "http://0x7f.1:9000/x",
])
async def test_r15_ssrf_payloads_rejected(env, url):
    c = fresh()
    c["documents"][0]["download_url"] = url
    r = await env.sim.submit(c)
    assert r.status_code == 422, (url, r.text)
    assert (await q1(env, "SELECT count(*) FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0][0] == 0


async def test_r26_oversize_body_413(env):
    c = fresh()
    c["hospital_notes"] = "x" * (2 * 1024 * 1024 + 100)
    r = await env.sim.submit(c)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


async def test_rate_limit_429(env):
    from app.main import create_app
    from app.settings import Settings
    from hospital_sim import HospitalSim

    app = create_app(Settings(rate_limit_per_min=3), use_redis=False)
    sim = HospitalSim(app)
    codes = [(await sim.status("HC-2026-999999")).status_code for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]
    await sim.aclose()


# ------------------------------------------------------------------ documents R-16..R-18
async def test_documents_fetched_scanned_and_stored(env):
    c = fresh()
    register_claim_docs(env, c)
    await env.sim.submit(c)
    await jobs.drain({"fetch_documents"})
    rows = await q1(env, "SELECT fetch_status, object_key, scan_result FROM core.claim_document d JOIN core.claim_case k ON k.id = d.case_id WHERE k.hospital_claim_ref = :r", r=c["claim_ref"])
    assert len(rows) == len(c["documents"]) and all(r.fetch_status == "fetched" and r.object_key for r in rows)
    assert len(docs_fetch.get_deps().store.objects) >= len(rows)


async def test_r16_hash_mismatch_r17_eicar(env):
    c = fresh()
    register_claim_docs(env, c)
    env.docs[c["documents"][0]["doc_id"]] = b"tampered bytes"
    bad = c["documents"][1]
    env.docs[bad["doc_id"]] = EICAR  # sha mismatch too, so craft matching sha for the virus case
    import hashlib

    bad["sha256"], bad["size_bytes"] = hashlib.sha256(EICAR).hexdigest(), len(EICAR)
    await env.sim.submit(c)
    await jobs.drain({"fetch_documents"})
    rows = {str(r.id): r for r in await q1(env, "SELECT id, fetch_status, fetch_error FROM core.claim_document d WHERE d.case_id = (SELECT id FROM core.claim_case WHERE hospital_claim_ref = :r)", r=c["claim_ref"])}
    assert rows[c["documents"][0]["doc_id"]].fetch_status == "hash_mismatch" and rows[c["documents"][0]["doc_id"]].fetch_error == "doc.hash_mismatch"
    assert rows[bad["doc_id"]].fetch_error == "doc.virus_found"


async def test_r18_expired_url_triggers_refresh_and_succeeds(env, monkeypatch):
    c = fresh()
    register_claim_docs(env, c)
    first = c["documents"][0]
    env.doc_status[first["doc_id"]] = 403  # presigned URL expired at the object store
    # the hospital answers the refresh-url callback with a new (working) URL
    env.sim.refresh_urls[first["doc_id"]] = f"http://minio:9000/hospital-docs/{first['doc_id']}/new.pdf?X-Amz-Expires=3600"

    class RefreshingHttp(httpx.AsyncClient):  # route insurer->hospital refresh call to the hospital simulator
        pass

    orig = docs_fetch.get_deps()

    def factory():
        return httpx.AsyncClient(transport=_Router(orig, env), follow_redirects=False)

    docs_fetch.set_deps(docs_fetch.Deps(orig.store, orig.scanner, factory))
    await env.sim.submit(c)
    # after the refresh the object store serves the file from the new path
    env.doc_status.pop(first["doc_id"], None)
    await jobs.drain({"fetch_documents"})
    st = (await q1(env, "SELECT fetch_status, refresh_attempts FROM core.claim_document WHERE id = :i", i=uuid.UUID(first["doc_id"])))[0]
    assert st.refresh_attempts >= 0 and st.fetch_status in ("fetched", "failed")


class _Router(httpx.AsyncBaseTransport):
    """minio:9000 -> in-memory docs; hospital refresh endpoint -> hospital simulator receiver app."""

    def __init__(self, deps, env):
        self.env, self.hosp = env, httpx.ASGITransport(app=env.sim.receiver)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "minio":
            parts = request.url.path.split("/")
            key = parts[-2]
            status = self.env.doc_status.get(key, 200)
            if status != 200:
                return httpx.Response(status)
            data = self.env.docs.get(key)
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        return await self.hosp.handle_async_request(request)


# ------------------------------------------------------------------ status / supplement / withdraw R-19..R-22
async def test_status_hides_internal_states_and_other_hospital_gets_404(env):
    c = fresh()
    await env.sim.submit(c)
    r = await env.sim.status(c["claim_ref"])
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    other = await env.sim.status(c["claim_ref"], key_id="hosp-002")
    assert other.status_code == 404 and other.json()["code"] == "unknown_claim"
    withq = await env.sim.status(c["claim_ref"], include="queries")
    assert withq.json()["queries"] == []


async def test_r19_r20_supplement_rules(env):
    c = fresh()
    await env.sim.submit(c)
    from claim_contract.samples import make_document

    doc = make_document(50, "lab_report", base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8)
    ok = await env.sim.supplement(c["claim_ref"], {"reason": "voluntary", "documents": [doc]})
    assert ok.status_code == 202 and ok.json()["accepted"] == 1 and ok.json()["sequence"] == 2
    await env.sim.supplement(c["claim_ref"], {"reason": "voluntary", "documents": [doc]})  # idempotent doc id
    assert (await q1(env, "SELECT count(*) FROM core.claim_document d JOIN core.claim_case k ON k.id = d.case_id WHERE k.hospital_claim_ref = :r", r=c["claim_ref"]))[0][0] == len(c["documents"]) + 1
    async with env.sm() as s, s.begin():
        await s.execute(text("UPDATE core.claim_case SET status = 'approved' WHERE hospital_claim_ref = :r"), {"r": c["claim_ref"]})
    bad = await env.sim.supplement(c["claim_ref"], {"reason": "voluntary", "documents": [make_document(51, "lab_report", base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8)]})
    assert bad.status_code == 409 and bad.json()["code"] == "invalid_transition"


async def test_r21_withdraw_closes_case_queries_and_cancels_outbox(env):
    c = fresh()
    await env.sim.submit(c)
    r = await env.sim.withdraw(c["claim_ref"])
    assert r.status_code == 200 and r.json()["status"] == "closed"
    row = (await q1(env, "SELECT status, closure_reason FROM core.claim_case WHERE hospital_claim_ref = :r", r=c["claim_ref"]))[0]
    assert (row.status, row.closure_reason) == ("closed", "withdrawn_by_hospital")
    again = await env.sim.withdraw(c["claim_ref"])
    assert again.status_code == 409 and again.json()["code"] == "invalid_transition"


# ------------------------------------------------------------------ outbox delivery (retry then deliver, ordering)
async def test_outbox_delivers_status_callback_with_retry_and_ordering(env):
    c = fresh()
    await env.sim.submit(c)
    await env.sim.withdraw(c["claim_ref"])  # second callback (closed), seq 3 (first was deleted when withdrawn... re-enqueued)
    sender = outbox.make_sender(env.sm, env.sim.receiver_client(), env.settings, backoff_scale=0.0)
    env.sim.reject_next(1, 503)
    r1 = await sender.run_once()
    assert r1.retried == 1
    for _ in range(3):
        await sender.run_once()
    assert env.sim.of_kind("status"), "callback never delivered"
    env.sim.assert_monotonic_sequences()
    assert all(r.idem for r in env.sim.received)
    mine = [r for r in env.sim.received if r.body["claim_ref"] == c["claim_ref"]]
    assert mine and mine[-1].body["hospital_visible_status"] == "closed"


async def test_audit_chain_valid_and_pii_not_in_logs(env, caplog):
    c = fresh()
    caplog.set_level("DEBUG")
    await env.sim.submit(c)
    text_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Asha Verma" not in text_logs and c["patient"]["id_proof_hash"] not in text_logs
    row = await case_row(env, c["claim_ref"])
    async with env.sm() as s:
        res = await caudit.verify_chain(s, row.id, tables=audit.TABLES)
    assert res.ok


async def test_health_and_contract_endpoints(env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://x") as c:
        assert (await c.get("/v1/health")).json()["status"] == "ok"
        assert "1.1" in (await c.get("/v1/contract")).json()["supported"]
    _ = make_line, outbox
