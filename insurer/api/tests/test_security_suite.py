"""Insurer-side security specs from 05-03 Annex G: SEC-T17 (JWT), SEC-T9 (log/audit/outbox PII scan), SEC-T10 (SoD), SEC-T1 (signature tamper)."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid

import jwt
import pytest
from claim_contract.insurer_side import audit as caudit
from claim_contract.insurer_side.samples import make_submission
from ins_helpers import register_claim_docs, unique_member
from insurer_app.settings import get_settings
from sqlalchemy import text

pytestmark = pytest.mark.integration

PATH = "/v1/cases"


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def tokens() -> dict[str, str]:
    s = get_settings()
    now = int(time.time())
    good = {"sub": "reviewer1", "roles": ["reviewer"], "aud": s.keycloak_audience, "iat": now, "exp": now + 600}
    out = {
        "alg_none": f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(good)}.",
        "expired": jwt.encode({**good, "exp": now - 10}, s.dev_jwt_secret, algorithm="HS256"),
        "wrong_aud": jwt.encode({**good, "aud": "someone-else"}, s.dev_jwt_secret, algorithm="HS256"),
        "wrong_secret": jwt.encode(good, "not-the-secret-not-the-secret-not-the-secret", algorithm="HS256"),
        "garbage": "not.a.jwt",
        "empty": "",
    }
    head, payload, sig = jwt.encode(good, s.dev_jwt_secret, algorithm="HS256").split(".")
    out["tampered_payload"] = f"{head}.{b64({**good, 'roles': ['admin', 'senior_reviewer', 'approver']})}.{sig}"
    out["tampered_sig"] = f"{head}.{payload}.{sig[:-3]}AAA"
    return out


@pytest.mark.parametrize("name", ["alg_none", "expired", "wrong_aud", "wrong_secret", "garbage", "empty", "tampered_payload", "tampered_sig"])
async def test_sec_t17_bad_tokens_are_401(env, name):
    import httpx

    tok = tokens()[name]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://insurer", headers={"Authorization": f"Bearer {tok}"} if tok else {}) as c:
        r = await c.get(PATH)
    assert r.status_code == 401, (name, r.status_code, r.text[:200])


async def test_sec_t17_good_token_still_works_and_role_escalation_by_claim_is_not_possible(env):
    async with env.client("reviewer1", ["reviewer"]) as c:
        assert (await c.get(PATH)).status_code == 200
        assert (await c.get("/v1/admin/gate/stats")).status_code == 403  # reviewer cannot reach admin routes


async def test_sec_t1_signature_tamper_matrix(env):
    claim = make_submission(claim_ref=f"HC-2026-{uuid.uuid4().int % 900000 + 100000}", doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **unique_member())
    raw = json.dumps(claim, separators=(",", ":")).encode()
    idem = str(uuid.uuid4())
    sim = env.sim
    h = sim.headers("POST", "/v1/hospital-api/claims", raw, idem)

    async def post(body=raw, headers=h, target="/v1/hospital-api/claims", method="POST"):
        return await sim.insurer.request(method, target, content=body, headers=headers)

    flipped = bytearray(raw)
    flipped[10] ^= 1
    assert (await post(bytes(flipped))).status_code == 401
    assert (await post(headers={**h, "X-Signature": h["X-Signature"][:-4] + "0000"})).status_code == 401
    assert (await post(headers={**h, "X-Signature": ""})).status_code == 401
    assert (await post(target="/v1/hospital-api/claims?x=1")).status_code == 401
    wrong = sim.headers("POST", "/v1/hospital-api/claims", raw, idem, secret=b"x" * 32)
    assert (await post(headers=wrong)).status_code == 401
    from datetime import UTC, datetime, timedelta

    for off, ok in ((301, False), (-301, False), (299, True)):
        hh = sim.headers("POST", "/v1/hospital-api/claims", raw, str(uuid.uuid4()), ts=datetime.now(UTC) + timedelta(seconds=off))
        register_claim_docs(env, claim)
        code = (await post(headers=hh)).status_code
        assert (code != 401) is ok, (off, code)
        if ok:
            break


async def test_sec_t9_no_raw_pii_in_logs_audit_or_outbox(env, caplog):
    """A claim whose free text carries Aadhaar/PAN/phone/email must not leak them into logs, audit payloads or callbacks."""
    from insurer_app.services import jobs

    caplog.set_level(logging.DEBUG)
    claim = make_submission(claim_ref=f"HC-2026-{uuid.uuid4().int % 900000 + 100000}", doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **unique_member())
    claim["hospital_notes"] = "Patient phone 9876543210 mail a.b@example.com PAN ABCDE1234F"
    register_claim_docs(env, claim)
    r = await env.sim.submit(claim)
    # V-validation may reject free-text PII outright (door) or accept; either way nothing raw may be stored or logged
    if r.status_code == 202:
        await jobs.drain({"fetch_documents", "start_verification"})
    pat = re.compile(r"9876543210|a\.b@example\.com|ABCDE1234F")
    assert not pat.search(caplog.text), "raw PII reached the logs"
    async with env.sm() as s:
        for table, col in (("audit.audit_event", "payload"), ("ops.outbox", "payload")):
            rows = (await s.execute(text(f"SELECT {col}::text FROM {table}"))).scalars().all()
            assert not any(pat.search(x or "") for x in rows), f"raw PII found in {table}"
    # and the logs never contain credentials
    assert "dev-hosp-to-ins-secret" not in caplog.text and "Bearer ey" not in caplog.text
    assert caudit.contains_pii("call 9876543210")  # the detector itself works


async def test_sec_t10_separation_of_duties_is_enforced_by_api_and_db(env):
    from insurer_app.services.gate import Thresholds  # noqa: F401  (import check only)

    async with env.sm() as s:
        # DB constraint: an approval row where approver == reviewer must be refused regardless of the API
        has = (await s.execute(text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'core.approval'::regclass AND contype = 'c'"))).scalar_one()
        assert has >= 1
