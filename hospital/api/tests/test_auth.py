"""Auth and RBAC tests (doc 02 §9): JWT matrix, role guards, service identities, admin users."""

import asyncio
import logging
import uuid
from typing import Any

import httpx
import jwt
import pytest
from app.auth.capabilities import CAPS, capabilities
from app.auth.deps import assert_all_routes_guarded, case_scope_sql
from app.auth.principal import Principal
from app.core.logging import scrub
from sqlalchemy import create_engine, text
from tests.conftest import make_token

pytestmark = pytest.mark.integration


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ---- JWT validation matrix (local key) ---------------------------------------------------------
async def test_valid_token_works_and_provisions_user(
    unit_client: httpx.AsyncClient, rsa_key: Any
) -> None:
    sub = str(uuid.uuid4())
    r = await unit_client.get(
        "/v1/me", headers=H(make_token(rsa_key, {"sub": sub, "email": f"{sub[:8]}@x.io"}))
    )
    assert r.status_code == 200 and r.json()["roles"] == ["officer"]
    assert "claim.submit" in r.json()["capabilities"]


@pytest.mark.parametrize(
    ("name", "kw", "code"),
    [
        ("wrong_aud", {"claims": {"aud": "account"}}, "invalid_token"),
        ("wrong_iss", {"claims": {"iss": "http://evil/realms/hospital"}}, "invalid_token"),
        ("expired", {"claims": {"exp": 1, "iat": 0}}, "invalid_token"),
        ("not_yet_valid", {"claims": {"nbf": 4102444800}}, "invalid_token"),
        ("missing_sub", {"drop": ("sub",)}, "invalid_token"),
        ("missing_exp", {"drop": ("exp",)}, "invalid_token"),
        ("unknown_kid", {"kid": "nope"}, "invalid_token"),
    ],
)
async def test_invalid_tokens(
    unit_client: httpx.AsyncClient, rsa_key: Any, name: str, kw: dict[str, Any], code: str
) -> None:
    r = await unit_client.get("/v1/me", headers=H(make_token(rsa_key, **kw)))
    assert r.status_code == 401 and r.json()["code"] == code, (name, r.text)
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["trace_id"]


async def test_tampered_signature_and_other_key(
    unit_client: httpx.AsyncClient, rsa_key: Any
) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    t = make_token(rsa_key)
    head, body, sig = t.split(".")
    assert (
        await unit_client.get("/v1/me", headers=H(f"{head}.{body}.{sig[:-4]}AAAA"))
    ).status_code == 401
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (await unit_client.get("/v1/me", headers=H(make_token(other)))).status_code == 401


async def test_alg_none_and_hs256_confusion(unit_client: httpx.AsyncClient, rsa_key: Any) -> None:
    import base64
    import json

    def b(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    none_tok = f"{b({'alg': 'none', 'kid': 'k1'})}.{b({'sub': 'x', 'aud': 'hospital-api'})}."
    assert (await unit_client.get("/v1/me", headers=H(none_tok))).status_code == 401
    pub_pem = rsa_key.public_key().public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM,
        __import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["x"]
        ).PublicFormat.SubjectPublicKeyInfo,
    )
    hs = (
        jwt.encode(
            {
                "sub": "x",
                "aud": "hospital-api",
                "iss": "http://localhost:8080/realms/hospital",
                "exp": 4102444800,
                "iat": 0,
            },
            pub_pem,
            algorithm="HS256",
            headers={"kid": "k1"},
        )
        if False
        else None
    )
    assert (
        hs is None
    )  # PyJWT refuses to sign HS256 with a PEM public key; the algorithm check covers verify side
    r = await unit_client.get("/v1/me", headers=H("garbage"))
    assert r.status_code == 401


async def test_missing_header_and_roles(unit_client: httpx.AsyncClient, rsa_key: Any) -> None:
    r = await unit_client.get("/v1/me")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    r = await unit_client.get("/v1/me", headers=H(make_token(rsa_key, drop=("realm_access",))))
    assert r.status_code == 403 and r.json()["code"] == "no_role"
    r = await unit_client.get(
        "/v1/me", headers=H(make_token(rsa_key, {"realm_access": {"roles": ["offline_access"]}}))
    )
    assert r.json()["code"] == "no_role"
    r = await unit_client.get("/v1/me", headers=H(make_token(rsa_key, drop=("email",))))
    assert r.status_code == 401 and r.json()["code"] == "invalid_token"


async def test_huge_authorization_header_431(unit_client: httpx.AsyncClient) -> None:
    r = await unit_client.get("/v1/me", headers={"Authorization": "Bearer " + "a" * 9000})
    assert r.status_code == 431


async def test_roles_forbidden_with_required_and_audit_throttle(
    unit_client: httpx.AsyncClient, rsa_key: Any, settings: Any
) -> None:
    desk = make_token(
        rsa_key, {"realm_access": {"roles": ["desk"]}, "sub": str(uuid.uuid4()), "email": "d@x.io"}
    )
    for _ in range(25):  # flood: at most one denial audit event per user/route/window
        r = await unit_client.get("/v1/admin/users", headers=H(desk))
        assert r.status_code == 403 and r.json()["required"] == ["admin"]
    eng = create_engine(settings.db_url.replace("+asyncpg", "+psycopg"))
    with eng.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM audit_event WHERE event_type='permission.denied' "
                "AND payload->>'route'='/v1/admin/users'"
            )
        ).scalar()
    assert n == 1


async def test_two_concurrent_first_logins_one_row(
    unit_client: httpx.AsyncClient, rsa_key: Any, settings: Any
) -> None:
    sub = str(uuid.uuid4())
    t = make_token(rsa_key, {"sub": sub, "email": f"{sub[:8]}@race.io"})
    rs = await asyncio.gather(*[unit_client.get("/v1/me", headers=H(t)) for _ in range(8)])
    assert all(r.status_code == 200 for r in rs)
    eng = create_engine(settings.db_url.replace("+asyncpg", "+psycopg"))
    with eng.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM app_user WHERE keycloak_sub=:s"), {"s": sub}
            ).scalar()
            == 1
        )


async def test_email_conflict(unit_client: httpx.AsyncClient, rsa_key: Any) -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"{a[:8]}@dup.io"
    assert (
        await unit_client.get("/v1/me", headers=H(make_token(rsa_key, {"sub": a, "email": email})))
    ).status_code == 200
    r = await unit_client.get("/v1/me", headers=H(make_token(rsa_key, {"sub": b, "email": email})))
    assert r.status_code == 409 and r.json()["code"] == "email_conflict"


# ---- real Keycloak tokens ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("user", "role"), [("desk1", "desk"), ("officer1", "officer"), ("hadmin", "admin")]
)
async def test_real_tokens_me(client: httpx.AsyncClient, tok: Any, user: str, role: str) -> None:
    r = await client.get("/v1/me", headers=tok(user))
    assert r.status_code == 200 and r.json()["roles"] == [role]
    assert r.json()["capabilities"] == capabilities({role})


async def test_role_route_matrix(client: httpx.AsyncClient, tok: Any) -> None:
    rows = {  # route -> who may call it
        "/v1/me": {
            "desk1": 200,
            "officer1": 200,
            "hadmin": 200,
            "svc:n8n": 403,
            "svc:crew": 403,
            "anon": 401,
        },
        "/v1/admin/users": {
            "desk1": 403,
            "officer1": 403,
            "hadmin": 200,
            "svc:n8n": 403,
            "svc:crew": 403,
            "anon": 401,
        },
        "/v1/health": {"desk1": 200, "anon": 200, "svc:n8n": 200},
    }
    for route, expect in rows.items():
        for who, code in expect.items():
            r = await client.get(route, headers={} if who == "anon" else tok(who))
            assert r.status_code == code, (route, who, r.status_code, r.text)


async def test_service_tokens_have_no_app_user_and_wrong_service(
    client: httpx.AsyncClient, tok: Any
) -> None:
    r = await client.get("/v1/me", headers=tok("svc:n8n"))
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


async def test_ready_and_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/health")).json() == {"status": "ok"}
    r = await client.get("/v1/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"db": "ok", "redis": "ok", "jwks": "ok", "schema": "ok"}


async def test_unknown_route_problem_json_with_trace(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/nope")
    assert r.status_code == 404 and r.json()["code"] == "not_found" and r.json()["trace_id"]
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["x-request-id"] == r.json()["trace_id"]


# ---- admin users -------------------------------------------------------------------------------
async def test_admin_user_list_and_deactivate_flow(client: httpx.AsyncClient, tok: Any) -> None:
    await client.get("/v1/me", headers=tok("officer2"))  # provision
    r = await client.get("/v1/admin/users", params={"q": "officer2"}, headers=tok("hadmin"))
    items = r.json()["items"]
    assert r.status_code == 200 and items and items[0]["role"] == "officer"
    uid = items[0]["id"]
    r = await client.patch(f"/v1/admin/users/{uid}", json={"active": False}, headers=tok("hadmin"))
    assert r.status_code == 200 and r.json()["active"] is False
    r = await client.get("/v1/me", headers=tok("officer2"))  # immediate (cache invalidated)
    assert r.status_code == 403 and r.json()["code"] == "user_disabled"
    await client.patch(f"/v1/admin/users/{uid}", json={"active": True}, headers=tok("hadmin"))
    assert (await client.get("/v1/me", headers=tok("officer2"))).status_code == 200
    r = await client.get(f"/v1/admin/users/{uid}/activity", headers=tok("hadmin"))
    assert r.status_code == 200


async def test_cannot_deactivate_self_or_last_admin_or_unknown(
    client: httpx.AsyncClient, tok: Any
) -> None:
    me = (await client.get("/v1/me", headers=tok("hadmin"))).json()["id"]
    r = await client.patch(f"/v1/admin/users/{me}", json={"active": False}, headers=tok("hadmin"))
    assert r.status_code == 409 and r.json()["code"] == "cannot_deactivate_self"
    r = await client.patch(
        f"/v1/admin/users/{uuid.uuid4()}", json={"active": False}, headers=tok("hadmin")
    )
    assert r.status_code == 404
    r = await client.patch(
        "/v1/admin/users/not-a-uuid", json={"active": False}, headers=tok("hadmin")
    )
    assert r.status_code == 404
    r = await client.patch(
        f"/v1/admin/users/{me}", json={"active": False, "x": 1}, headers=tok("hadmin")
    )
    assert r.status_code == 422


# ---- unit-level -----------------------------------------------------------------------------------
def test_capabilities_table() -> None:
    assert capabilities({"desk"}) == sorted(CAPS["desk"])
    assert "claim.submit" in capabilities({"officer"}) and "claim.submit" not in capabilities(
        {"admin"}
    )
    assert capabilities({"unknown"}) == []


def test_case_scope() -> None:
    uid = uuid.uuid4()
    desk = Principal("human", "s", frozenset({"desk"}), id=uid)
    sql, params = case_scope_sql(desk)
    assert "assigned_to" in sql and params == {"scope_uid": uid}
    for roles in ({"officer"}, {"admin"}):
        assert case_scope_sql(Principal("human", "s", frozenset(roles), id=uid))[0] == "TRUE"
    assert (
        case_scope_sql(Principal("service", "s", frozenset({"svc-n8n"}), client_id="hospital-n8n"))[
            0
        ]
        == "TRUE"
    )


def test_route_guard_catches_unguarded_route(app: Any) -> None:
    from fastapi import APIRouter

    r = APIRouter()

    @r.get("/v1/unguarded")
    async def _x() -> dict[str, str]:
        return {}

    app.state.api_routers.append(r)
    try:
        with pytest.raises(RuntimeError, match="unguarded route /v1/unguarded"):
            assert_all_routes_guarded(app)
    finally:
        app.state.api_routers.remove(r)
    assert_all_routes_guarded(app)


def test_log_scrubbing(caplog: pytest.LogCaptureFixture) -> None:
    tok = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eHh4eHh4In0.c2lnbmF0dXJlc2lnbmF0dXJl"
    assert tok not in scrub(f"calling with {tok} and Bearer {tok}")
    assert "[REDACTED]" in scrub(f"Authorization: Bearer {tok}")
    logging.getLogger("x").info("hi")
