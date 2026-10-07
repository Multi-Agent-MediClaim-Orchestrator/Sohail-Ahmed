"""Reviewer / approver / admin / service authentication (Keycloak JWT; HS256 dev tokens when no JWKS is set).

Roles: ``reviewer``, ``senior_reviewer``, ``approver``, ``admin``; service accounts carry ``n8n-service`` etc."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import jwt
from claim_contract.errors import ProblemError
from fastapi import Depends, Request

from ..settings import Settings, get_settings

ALL_STAFF = ("reviewer", "senior_reviewer", "approver", "admin")


@dataclass(frozen=True)
class Principal:
    sub: str
    roles: frozenset[str]
    name: str = ""
    service: bool = False
    claims: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def has_any(self, roles: Iterable[str]) -> bool:
        r = set(roles)
        return bool(self.roles & r)

    @property
    def primary_role(self) -> str:
        for r in ("senior_reviewer", "approver", "reviewer", "admin", "n8n-service"):
            if r in self.roles:
                return r
        return next(iter(sorted(self.roles)), "none")


_jwks: jwt.PyJWKClient | None = None


def _decode(token: str, s: Settings) -> dict[str, Any]:
    global _jwks
    try:
        if s.keycloak_jwks_url and not (s.allow_dev_tokens and jwt.get_unverified_header(token).get("alg") == "HS256"):
            if _jwks is None:
                _jwks = jwt.PyJWKClient(s.keycloak_jwks_url)
            key = _jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["RS256"], audience=s.keycloak_audience, issuer=s.keycloak_issuer or None,
                              options={"verify_iss": bool(s.keycloak_issuer)})
        return jwt.decode(token, s.dev_jwt_secret, algorithms=["HS256"], audience=s.keycloak_audience)
    except jwt.PyJWTError as exc:
        raise ProblemError("invalid_signature", "invalid or expired token", status=401) from exc


def principal_from_token(token: str, s: Settings | None = None) -> Principal:
    s = s or get_settings()
    claims = _decode(token, s)
    roles = set(claims.get("roles") or []) | set((claims.get("realm_access") or {}).get("roles") or [])
    sub = str(claims.get("sub") or claims.get("preferred_username") or "")
    if not sub:
        raise ProblemError("invalid_signature", "token has no subject", status=401)
    # service accounts: dev tokens use a `svc-...` subject; Keycloak client-credentials tokens carry a `svc-...` realm role
    svc_names = [sub] if sub.startswith("svc-") else sorted(r for r in roles if r.startswith("svc-"))
    svc = bool(svc_names)
    if svc:
        roles.add("n8n-service" if any("n8n" in n for n in svc_names) else "service")
    return Principal(sub=sub, roles=frozenset(roles), name=str(claims.get("name") or sub), service=svc, claims=claims)


def make_dev_token(sub: str, roles: list[str], *, s: Settings | None = None, ttl: int = 3600, name: str | None = None) -> str:
    """Dev/test helper: mint an HS256 token the API accepts when no JWKS is configured."""
    s = s or get_settings()
    now = int(time.time())
    return jwt.encode({"sub": sub, "roles": roles, "name": name or sub, "aud": s.keycloak_audience, "iat": now, "exp": now + ttl},
                      s.dev_jwt_secret, algorithm="HS256")


async def current_principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else request.query_params.get("access_token", "")
    if not token:
        raise ProblemError("invalid_signature", "authentication required", status=401)
    p = principal_from_token(token)
    request.state.principal = p
    return p


def require_roles(*roles: str) -> Callable[..., Any]:
    allowed = set(roles)

    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has_any(allowed):
            raise ProblemError("forbidden_role", f"requires one of {sorted(allowed)}", status=403)
        return p

    dep.allowed_roles = frozenset(allowed)  # type: ignore[attr-defined]  # introspected by the authz-matrix test (every route must declare roles)
    return dep


require_internal = require_roles("n8n-service", "service")
require_staff = require_roles("reviewer", "senior_reviewer", "approver")
