"""Authorisation matrix generated from the live route table (05-03 Annex C/I): every non-HMAC route must declare roles, and each role is
checked against it. ``security/authz-matrix.csv`` is (re)written by this test so reviews can diff it."""

from __future__ import annotations

import csv
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
ALL_ROLES = ["reviewer", "senior_reviewer", "approver", "admin", "n8n-service", "guest"]
PUBLIC = {"/v1/health", "/v1/ready", "/v1/contract", "/metrics", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
HMAC_PREFIXES = ("/v1/hospital-api/", "/internal/settlement/bank-callback")
OPEN_BY_DESIGN = {"/v1/client-errors"}  # error sink; rate limited, never returns data


def all_routes() -> list[APIRoute]:
    from app.routers import admin, decisions, events, internal, queries, reviewer, settlement

    routers = [reviewer.router, decisions.router, events.router, internal.router, queries.router, queries.internal, settlement.router, admin.router]
    return [r for rt in routers for r in rt.routes if isinstance(r, APIRoute)]


def roles_of(route: APIRoute) -> frozenset[str] | None:
    found: set[frozenset[str]] = set()

    def walk(dep) -> None:
        for sub in dep.dependencies:
            r = getattr(sub.call, "allowed_roles", None)
            if r is not None:
                found.add(r)
            walk(sub)

    walk(route.dependant)
    return frozenset().union(*found) if found else None  # all declared role checks on the route must pass -> intersection would be stricter; union is the caller-visible set


def concrete(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


def test_every_route_declares_roles_or_is_explicitly_exempt(env):
    undeclared = []
    for r in all_routes():
        if r.path in PUBLIC or r.path in OPEN_BY_DESIGN or r.path.startswith(HMAC_PREFIXES):
            continue
        if roles_of(r) is None:
            undeclared.append(f"{sorted(r.methods)} {r.path}")
    assert not undeclared, "routes without a role check:\n" + "\n".join(undeclared)


async def test_matrix_is_enforced_for_every_route_and_role(env):
    rows = []
    checked = 0
    for r in all_routes():
        if r.path in PUBLIC or r.path in OPEN_BY_DESIGN or r.path.startswith(HMAC_PREFIXES) or r.path == "/v1/events/stream":
            continue
        allowed = roles_of(r)
        assert allowed is not None, r.path
        method = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))[0]
        for role in ALL_ROLES:
            async with env.client(f"u-{role}", [role]) as c:
                resp = await c.request(method, concrete(r.path))
            is_allowed = role in allowed
            rows.append({"system": "insurer", "role": role, "method": method, "path": r.path, "allowed": "yes" if is_allowed else "no"})
            if is_allowed:
                assert resp.status_code != 403 and resp.status_code != 401, (role, method, r.path, resp.status_code)
            else:
                assert resp.status_code == 403, (role, method, r.path, resp.status_code, resp.text[:120])
            checked += 1
        async with __import__("httpx").AsyncClient(transport=__import__("httpx").ASGITransport(app=env.app), base_url="http://x") as anon:
            assert (await anon.request(method, concrete(r.path))).status_code == 401, ("anonymous", r.path)
    out = ROOT / "security" / "authz-matrix.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["system", "role", "method", "path", "allowed"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["path"], x["method"], x["role"])))
    assert checked > 150


async def test_idor_other_hospitals_claim_is_not_visible(env):
    from app.services import jobs
    from claim_contract.samples import make_submission
    from ins_helpers import register_claim_docs, unique_member

    c = make_submission(claim_ref=f"HC-2026-{uuid.uuid4().int % 900000 + 100000}", hospital_id="HOSP-0001", doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **unique_member())
    register_claim_docs(env, c)
    assert (await env.sim.submit(c, key_id="hosp-001")).status_code == 202
    await jobs.drain({"fetch_documents", "start_verification"})
    other = await env.sim.status(c["claim_ref"], key_id="hosp-002")
    assert other.status_code in (403, 404)
    assert (await env.sim.status(c["claim_ref"], key_id="hosp-001")).status_code == 200
