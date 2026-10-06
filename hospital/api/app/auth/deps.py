"""Authentication and authorisation dependencies. Deny by default: every non-public route must
depend on one of the GUARDS (checked by `assert_all_routes_guarded`)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import jwt
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute

from app.auth.capabilities import HUMAN_ROLES, SERVICE_ROLES, primary_role
from app.auth.principal import Principal
from app.auth.users import resolve_user
from app.core.config import Settings
from app.core.errors import ApiError, Forbidden, Unauthenticated
from app.core.uow import UoW
from app.services import audit

log = logging.getLogger("app.auth")
_BEARER = re.compile(r"^Bearer\s+(\S+)$", re.I)
SERVICE_CLIENTS = {"hospital-n8n", "hospital-crew", "hospital-internal"}


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    fn.__guard__ = True  # type: ignore[attr-defined]
    return fn


async def decode_token(request: Request, token: str) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise Unauthenticated("invalid_token", "malformed token") from None
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise Unauthenticated("invalid_token", "unsupported algorithm or missing kid")
    key = await request.app.state.jwks.get(header["kid"])
    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=s.oidc_audience,
            issuer=s.oidc_issuer,
            leeway=s.jwt_leeway_s,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        raise ApiError(
            "invalid_token",
            "token expired",
            headers={
                "WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"'
            },
        ) from None
    except jwt.PyJWTError as e:
        raise Unauthenticated("invalid_token", type(e).__name__) from None


@guard
async def current_principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    m = _BEARER.match(request.headers.get("authorization", ""))
    if not m:
        raise Unauthenticated("unauthenticated", "missing bearer token")
    claims = await decode_token(request, m.group(1))
    realm_roles = set(claims.get("realm_access", {}).get("roles", []))
    s: Settings = request.app.state.settings
    azp = claims.get("azp") or claims.get("client_id")

    if realm_roles & SERVICE_ROLES and azp in SERVICE_CLIENTS and not realm_roles & HUMAN_ROLES:
        p = Principal(
            "service", claims["sub"], frozenset(realm_roles & SERVICE_ROLES), client_id=azp
        )
    else:
        roles = realm_roles & HUMAN_ROLES
        if not roles:
            raise Forbidden("no_role", "token carries no hospital role")
        if not claims.get("email") or not claims.get("name"):
            raise Unauthenticated(
                "invalid_token", "email/name claims missing (mapper misconfigured)"
            )
        role = primary_role(roles)
        uid, active, first = await resolve_user(
            request.state.auth_session,
            request.app.state.redis,
            sub=claims["sub"],
            email=claims["email"],
            name=claims["name"],
            role=role,
            ttl=s.user_active_cache_s,
        )
        if not active:
            raise Forbidden("user_disabled", "this account is disabled")
        p = Principal(
            "human",
            claims["sub"],
            frozenset(roles),
            id=uid,
            email=claims["email"],
            name=claims["name"],
            role=role,
        )
        if first and await request.app.state.redis.set(
            f"cache:hosp:login:{claims.get('sid', claims['sub'])}", "1", nx=True, ex=8 * 3600
        ):
            await _audit_global(request, "user.login", {"user_id": str(uid), "role": role}, p)
    request.state.principal = p
    return p


async def _audit_global(
    request: Request, event: str, payload: dict[str, Any], p: Principal
) -> None:
    async with request.app.state.sessionmaker() as session:
        await audit.append(
            session, audit.SYSTEM_CASE, event, payload, actor_type=p.actor_type, actor_id=p.actor_id
        )
        await session.commit()


async def _audit_denied(request: Request, p: Principal) -> None:
    s: Settings = request.app.state.settings
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    ok = await request.app.state.redis.set(
        f"cache:hosp:deny:{p.actor_id}:{request.method}:{path}",
        "1",
        nx=True,
        ex=s.deny_audit_window_s,
    )
    if ok:
        await _audit_global(
            request, "permission.denied", {"method": request.method, "route": path}, p
        )


def require_access(
    *, humans: tuple[str, ...] = (), services: tuple[str, ...] = ()
) -> Callable[..., Any]:
    """Guard: humans need one of `humans` roles; services need one of `services` roles."""

    @guard
    async def dep(request: Request, p: Principal = Depends(current_principal)) -> Principal:
        if p.kind == "human":
            if not (p.roles & set(humans)):
                await _audit_denied(request, p)
                raise Forbidden(
                    "forbidden",
                    f"This action requires role {' or '.join(humans) or 'service'}",
                    required=list(humans),
                )
        else:
            if not services:
                raise Forbidden("forbidden", "human-only route", required=list(humans))
            if not (p.roles & set(services)):
                raise Forbidden("wrong_service", "this service identity may not call this route")
        return p

    return dep


def require_role(*allowed: str) -> Callable[..., Any]:
    return require_access(humans=allowed)


def require_service(*allowed: str) -> Callable[..., Any]:
    return require_access(services=allowed)


class AuthSessionMiddleware:
    """Gives `current_principal` its own short-lived session (the request UoW is separate)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app = scope["app"]
        async with app.state.sessionmaker() as session:
            scope.setdefault("state", {})["auth_session"] = session
            await self.app(scope, receive, send)


PUBLIC = {"/v1/health", "/v1/ready", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def _walk(dep: Any) -> Any:
    yield dep
    for d in dep.dependencies:
        yield from _walk(d)


def assert_all_routes_guarded(app: FastAPI, extra_public: set[str] | None = None) -> None:
    """Every route of every registered router must depend on a guard (or be public)."""
    public = PUBLIC | (extra_public or set())
    for router in app.state.api_routers:
        for r in router.routes:
            if not isinstance(r, APIRoute) or r.path in public:
                continue
            if not any(getattr(d.call, "__guard__", False) for d in _walk(r.dependant)):
                raise RuntimeError(f"unguarded route {r.path}")


def case_scope_sql(p: Principal, alias: str = "c") -> tuple[str, dict[str, Any]]:
    """SQL fragment + params restricting rows for Desk (own + unassigned); others unrestricted."""
    if p.kind == "service" or (p.roles & {"officer", "admin"}):
        return "TRUE", {}
    return f"({alias}.assigned_to = :scope_uid OR {alias}.assigned_to IS NULL)", {"scope_uid": p.id}


__all__ = ["UoW"]
