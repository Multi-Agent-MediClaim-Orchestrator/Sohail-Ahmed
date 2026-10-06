# 02-02 — hospital-api: Auth, Keycloak OIDC and RBAC (+ API skeleton)

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/{main.py,core/,auth/,routers/me.py,routers/admin_users.py}`, `infra/keycloak/`.

## 1. Goal
Stand up the FastAPI application skeleton and secure it end to end:
- Keycloak realm `hospital` with OIDC login for the UI (Authorization Code + PKCE).
- JWT validation in the API (RS256, JWKS cache), just-in-time user provisioning.
- Role-based access (Desk, Officer, Admin) with a deny-by-default route policy.
- Service accounts for n8n and crew (client-credentials), and HMAC-only access for insurer callbacks.
- A uniform request pipeline: structured logging, trace ids, RFC 7807 errors, audit hooks.

Out of scope: insurer-side auth (Dev B has its own `insurer` realm, see `03-dev-B-insurer/`), Keycloak UI theming, SSO federation, MFA (documented as an extension in §8).

## 2. Inputs / Outputs
- In: Keycloak realm export (`infra/keycloak/realm-hospital.json`), JWTs (RS256) from Keycloak, HMAC-signed callbacks from the insurer (validated by `claim_contract.signing`).
- Out:
  - `CurrentUser` dependency and `require_role()` guards.
  - `require_service()` guard for n8n/crew calls.
  - `case_scope()` row filter used by all case queries.
  - Audit events for first login per session and for permission denials.
  - Endpoints `/v1/me`, `/v1/admin/users*`, `/v1/health`, `/v1/ready`.
  - `hospital/api/openapi.json` published by CI (consumed by doc 10).

## 3. Data model
Uses `app_user` (doc 01 §3.3.1). Users are provisioned just-in-time on first valid token: `upsert_user(sub, email, name, role_from_token)`. Roles come only from the token claim `realm_access.roles`; the DB `role` column is a cache for reporting and for assignment dropdowns.

Redis keys (no SQL): `user:{sub}:active` (30 s cache of `app_user.active`), `jwks:{kid}` (JWKS cache), `deny:{user_id}:{route}` (rate-limit for denial audit events, 60 s).

### 3.1 Roles and capabilities (PROPOSED)
| Capability | Desk | Officer | Admin |
|---|---|---|---|
| Create case, upload docs, request docs | ✔ | ✔ | – |
| View all cases | own + unassigned | ✔ | ✔ (read) |
| Edit claim draft | – | ✔ | – |
| Sign off / submit claim | – | ✔ | – |
| Approve query reply | – | ✔ | – |
| Waive document requirement | – | ✔ | – |
| Edit config, users, thresholds | – | – | ✔ |
| Publish config (two-person rule) | – | – | ✔ (two different admins) |
| View audit | own cases | ✔ | ✔ |
| Assign cases | – | ✔ | ✔ |
| Deactivate users | – | – | ✔ |

Service identities (not human roles):
| Identity | Mechanism | Allowed |
|---|---|---|
| `svc-n8n` | Keycloak client `hospital-n8n`, client-credentials JWT | all `/v1/internal/*` plus read of cases/documents |
| `svc-crew` | Keycloak client `hospital-crew`, client-credentials JWT | `/v1/internal/crew/*`, read of masked documents |
| `svc-insurer-callback` | HMAC signature (`X-Key-Id`, `X-Signature`) | `/v1/insurer-callbacks/*` only |

### 3.2 Token claim contract
Access token (10 min) must contain: `sub`, `iss=http://localhost:8080/realms/hospital`, `aud` includes `hospital-api`, `exp`, `iat`, `email`, `name`, `realm_access.roles`, `azp` (authorised party = client id), and for service accounts `client_id` and role `svc-*`. Missing `email`/`name` for human tokens → 401 `invalid_token` (mapper misconfigured).

### 3.3 Keycloak realm layout
| Item | Value |
|---|---|
| Realm | `hospital` |
| Clients | `hospital-ui` (public, PKCE S256, redirect `http://localhost:3000/*`, web origin `http://localhost:3000`), `hospital-api` (bearer-only), `hospital-n8n` (confidential, service account), `hospital-crew` (confidential, service account) |
| Realm roles | `desk`, `officer`, `admin`, `svc-n8n`, `svc-crew` |
| Seed users | `desk1..3`, `officer1..3`, `admin1..3` (password from `.env`, `temporary=false` in dev) |
| Token lifetimes | access 10 min, refresh 8 h (session idle 30 min), SSO session max 12 h |
| Password policy | length 12, not username |
| Brute force | enabled: 5 failures → 60 s lock |
| Mappers | audience mapper (`hospital-api`), realm roles → `realm_access.roles`, `email`, `name` |

## 4. API / endpoints
```
GET   /v1/health                       public: liveness {status:"ok"}
GET   /v1/ready                        public: readiness (DB, Redis, JWKS reachable)
GET   /v1/me                           any authenticated human
GET   /v1/admin/users                  admin
PATCH /v1/admin/users/{id}             admin: {active:boolean}   (roles are managed in Keycloak)
GET   /v1/admin/users/{id}/activity    admin: last N audit events by this user
```
There is **no** token-introspection endpoint; n8n and crew use client-credentials JWTs validated like any other token.

### 4.1 `GET /v1/me` → 200
```json
{"id":"0190d3b8-...","email":"officer1@hospital.local","name":"Officer One","roles":["officer"],
 "capabilities":["case.create","case.view_all","draft.edit","claim.submit","query.approve","doc.waive","case.assign"]}
```
`capabilities` is derived server-side from the table in §3.1 so the UI never re-implements it.

### 4.2 `GET /v1/admin/users?role=&active=&q=&page=&size=` → 200
```json
{"items":[{"id":"...","email":"desk1@hospital.local","display_name":"Desk One","role":"desk","active":true,"last_login_at":"2026-10-06T09:01:12Z"}],
 "page":1,"size":25,"total":9}
```

### 4.3 `PATCH /v1/admin/users/{id}`
Request `{"active":false}` → 200 returns the updated user. Rules: an admin cannot deactivate themselves (409 `cannot_deactivate_self`); the last active admin cannot be deactivated (409 `last_admin`). Effect: Redis key `user:{sub}:active` deleted so the change is immediate; audit event `user.deactivated`.

### 4.4 Error responses (RFC 7807, per 01-01 §7)
| Status | `code` | When |
|---|---|---|
| 401 | `unauthenticated` | No `Authorization` header |
| 401 | `invalid_token` | Bad signature, expired, wrong `aud`/`iss`, missing claims; header `WWW-Authenticate: Bearer error="invalid_token"` |
| 403 | `forbidden` | Role not allowed; body includes `"required":["officer"]` |
| 403 | `no_role` | Valid token but no hospital role |
| 403 | `user_disabled` | `app_user.active = false` |
| 403 | `wrong_service` | Service token for another client used on a service route |
| 404 | `not_found` | Unknown user id |
| 409 | `cannot_deactivate_self`, `last_admin` | See 4.3 |

Example 403
```json
{"type":"https://claims.local/errors/forbidden","title":"Forbidden","status":403,"code":"forbidden",
 "detail":"This action requires role officer","required":["officer"],"trace_id":"c0ffee..."}
```

## 5. Build tasks
1. **Keycloak realm** `infra/keycloak/realm-hospital.json` (generated by `infra/keycloak/gen_realm.py` from `seed/users.json` so subs match doc 01 seeds): realm, four clients, five realm roles, 9 users, mappers, lifetimes, brute-force settings.
2. **Compose entry** `keycloak` (Dev A owns; `start-dev` for local, `--import-realm`, volume `keycloak-data`, healthcheck `/health/ready`, `mem_limit: 768m`). Also an `insurer` realm placeholder is Dev B's.
3. `hospital/api/app/main.py`: app factory `create_app()`, lifespan (DB pool, Redis, httpx client, JWKS prefetch), routers mounted under `/v1`, CORS limited to `HOSP_CORS_ORIGINS`, GZip.
4. `core/config.py`: pydantic-settings reading `HOSP_*` env, validated at startup (fail fast on missing issuer or secrets).
5. `core/errors.py`: exception classes (`Unauthenticated`, `Forbidden`, `NotFound`, `Conflict`, `Unprocessable`) → RFC 7807 handlers (reuse `claim_contract.errors`).
6. `core/logging.py`: structlog JSON; middleware sets `X-Request-ID`/`trace_id`, logs method, path, status, duration, user id (never bodies or tokens).
7. `auth/jwks.py`: fetch and cache JWKS (TTL 10 min, refetch once on unknown `kid` with backoff, serve stale up to 1 h if Keycloak is down).
8. `auth/deps.py`:
```python
async def current_user(
    token=Depends(oauth2_scheme), db=Depends(get_db), redis=Depends(get_redis)
) -> CurrentUser:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise Unauthenticated("invalid_token")
    key = await jwks.get(header["kid"])
    claims = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        audience=settings.oidc_audience,
        issuer=settings.oidc_issuer,
        leeway=30,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )
    roles = set(claims.get("realm_access", {}).get("roles", [])) & HUMAN_ROLES
    if not roles:
        raise Forbidden("no_role")
    user = await users.upsert(
        db, sub=claims["sub"], email=claims["email"], name=claims["name"], role=primary_role(roles)
    )
    if not await is_active(redis, db, user):
        raise Forbidden("user_disabled")
    return CurrentUser(id=user.id, sub=claims["sub"], roles=roles, email=user.email)


def require_role(*allowed):
    async def dep(u: CurrentUser = Depends(current_user)):
        if not (u.roles & set(allowed)):
            await audit_denied(u, request)  # rate-limited
            raise Forbidden(required=list(allowed))
        return u

    return dep
```
9. `auth/service.py`: `require_service("svc-n8n", "svc-crew")` validating client-credentials tokens (`azp` in allowed clients, role present, `aud` includes `hospital-api`).
10. `auth/hmac_dep.py`: `require_insurer_signature` wrapping `claim_contract.signing.verify` plus idempotency middleware; used only by `/v1/insurer-callbacks/*` (implemented in doc 06).
11. Row-level scope helper `case_scope(user)` returning a SQLAlchemy filter (Desk: `assigned_to = me OR assigned_to IS NULL`; Officer/Admin: no filter). Used by all case, document, query, audit reads. Service identities get no filter.
12. Capabilities map `auth/capabilities.py` (single dict role → set of capability strings) used by `/v1/me` and by the UI via OpenAPI types.
13. Routers: `/v1/me`, `/v1/admin/users`, `/v1/admin/users/{id}`, `/v1/admin/users/{id}/activity`.
14. Audit hooks: `user.login` (first token seen per `sid` claim or per 8 h), `permission.denied` (rate limited 1/min/user/route), `user.deactivated`.
15. Dockerfile (python:3.12-slim, non-root uid 10001, uvicorn workers=2, `--proxy-headers`), `/v1/health` and `/v1/ready`.
16. OpenAPI customisation: OAuth2 security scheme, tags per module, operation ids stable (`getMe`, `listUsers`); export to `hospital/api/openapi.json` in CI; fail CI if the file changes without commit.
17. Local dev helper `scripts/get_token.sh <user>` using direct grant (dev realm only) for curl/testing; `scripts/get_service_token.sh <client>`.
18. Startup route-guard test hook: `app.state.assert_all_routes_guarded()` (see §6).
19. `make keycloak-reset` target: drops volume, re-imports realm, re-seeds users.

### 5.1 Task-level definition of done
| Task | Done when |
|---|---|
| 1-2 Keycloak realm and compose | `docker compose up keycloak` healthy; `get_token.sh officer1` returns a JWT with `realm_access.roles=["officer"]` and `aud` containing `hospital-api` |
| 3-6 App skeleton | `GET /v1/health` 200; unknown route returns RFC 7807 404 with `trace_id`; logs are single-line JSON |
| 7-9 Auth deps | JWT matrix tests pass; service token accepted only by `require_service` |
| 10 HMAC dependency | contract signing vectors verify; stale and tampered requests rejected |
| 11-12 Scope and capabilities | `/v1/me` capabilities equal §3.1 table; scope filter unit tests pass |
| 13-14 Routers and audit hooks | admin PATCH invalidates Redis cache; denial audit rate limit verified |
| 15-17 Docker, OpenAPI, token scripts | image runs as non-root; `openapi.json` regenerated without diff in CI |
| 18-19 Guard test and reset target | adding an unguarded test route makes the guard test fail; `make keycloak-reset` leaves a working login |

## 6. Key logic
- Always validate `aud`, `iss`, `exp`, `nbf`/`iat` with 30 s leeway; reject `alg=none`/HS*; require `kid`.
- Token revocation: short TTL; admin deactivation also checked in DB on each request (cached 30 s in Redis `user:{sub}:active`; invalidated on PATCH).
- Two-person rule helper `require_distinct_actors(config_version_id, current_user)` used by config publish (doc 04/admin) and query round 3 approval (doc 07).
- **Deny by default**: every router declares an auth dependency; a startup test iterates `app.routes` and fails if any non-public route lacks one:
```python
PUBLIC = {"/v1/health", "/v1/ready", "/openapi.json", "/docs"}


def assert_all_routes_guarded(app):
    for r in app.routes:
        if r.path in PUBLIC or not hasattr(r, "dependant"):
            continue
        deps = {d.call for d in walk(r.dependant)}
        if not deps & GUARDS:
            raise RuntimeError(f"unguarded route {r.path}")
```
- Role precedence when a user has several roles: `admin > officer > desk` for the cached `app_user.role`; permissions check the full set.
- Service tokens never create `app_user` rows; audit `actor_id` is the client id (`svc-n8n`).
- Insurer callback requests: HMAC verified before any body parsing beyond hashing; `X-Key-Id` selects secret; replays handled by idempotency store (doc 06).
- Logging hygiene: tokens, `Authorization` headers and `Set-Cookie` are scrubbed by a structlog processor.

Sequence for a UI request:
```
Browser --(code+PKCE)--> Keycloak --> access token --> Next.js route handler (stores in httpOnly cookie)
Next.js --Bearer--> hospital-api: validate (JWKS) -> upsert user -> role check -> handler -> audit
```

### 6.1 Reference implementations (copy-ready skeletons)

`auth/capabilities.py`
```python
CAPS: dict[str, set[str]] = {
    "desk": {"case.create", "case.view_own", "doc.upload", "doc.request", "audit.view_own"},
    "officer": {
        "case.create",
        "case.view_all",
        "doc.upload",
        "doc.request",
        "doc.waive",
        "draft.edit",
        "claim.submit",
        "query.approve",
        "case.assign",
        "audit.view_all",
    },
    "admin": {
        "case.view_all",
        "config.edit",
        "config.publish",
        "user.manage",
        "case.assign",
        "audit.view_all",
    },
}


def capabilities(roles: set[str]) -> list[str]:
    return sorted(set().union(*(CAPS[r] for r in roles if r in CAPS)))
```

`auth/jwks.py`
```python
class JwksCache:
    def __init__(self, url, ttl=600, stale_max=3600): ...
    async def get(self, kid: str):
        if (k := self._keys.get(kid)) and not self._expired():
            return k
        if self._can_refetch():  # at most once per 10 s
            try:
                await self._fetch()
                self._last_ok = now()
            except httpx.HTTPError:
                if self._keys.get(kid) and now() - self._last_ok < self.stale_max:
                    return self._keys[kid]
                raise Unauthenticated("jwks_unavailable")
        if kid in self._keys:
            return self._keys[kid]
        raise Unauthenticated("invalid_token", "unknown kid")
```

`auth/users.py::upsert`
```sql
INSERT INTO app_user (id, keycloak_sub, email, display_name, role, last_login_at)
VALUES (:id, :sub, :email, :name, :role, now())
ON CONFLICT (keycloak_sub) DO UPDATE
  SET email = EXCLUDED.email, display_name = EXCLUDED.display_name, role = EXCLUDED.role,
      last_login_at = CASE WHEN app_user.last_login_at < now() - interval '5 minutes' THEN now() ELSE app_user.last_login_at END
RETURNING *;
```
(`last_login_at` is only touched every 5 minutes so the upsert does not write on every request; the whole upsert is skipped when the Redis `user:{sub}:profile` cache, TTL 60 s, still matches the token's email/name/role.)

Request pipeline order (outermost first): `RequestIdMiddleware` → `AccessLogMiddleware` → `CORSMiddleware` → exception handlers → router dependency (`current_user` or `require_service` or `require_insurer_signature`) → handler. Errors raised in dependencies are rendered by the same RFC 7807 handler so every failure has `trace_id`.

### 6.2 Keycloak client definitions (excerpt of `realm-hospital.json`)
```json
{"clientId":"hospital-ui","publicClient":true,"standardFlowEnabled":true,"directAccessGrantsEnabled":false,
 "redirectUris":["http://localhost:3000/*"],"webOrigins":["http://localhost:3000"],
 "attributes":{"pkce.code.challenge.method":"S256","post.logout.redirect.uris":"http://localhost:3000/*"}},
{"clientId":"hospital-api","bearerOnly":true},
{"clientId":"hospital-n8n","publicClient":false,"serviceAccountsEnabled":true,"standardFlowEnabled":false,
 "secret":"${HOSP_N8N_CLIENT_SECRET}","defaultRoles":["svc-n8n"]},
{"clientId":"hospital-crew","publicClient":false,"serviceAccountsEnabled":true,"standardFlowEnabled":false,
 "secret":"${HOSP_CREW_CLIENT_SECRET}","defaultRoles":["svc-crew"]}
```
`directAccessGrantsEnabled` is true only in the dev override file `realm-hospital.dev.json` (used by `scripts/get_token.sh`) and must be false in the base realm.

### 6.3 Sequence diagrams
Human login and API call:
```
UI(Next.js) -> Keycloak: /auth?response_type=code&code_challenge=...&scope=openid
Keycloak -> UI: redirect with code
UI route handler -> Keycloak: /token (code + verifier)  => access_token, refresh_token (httpOnly cookies)
UI -> hospital-api: GET /v1/me  Authorization: Bearer <access>
hospital-api: jwks.get(kid) -> verify -> upsert user -> capabilities -> 200
```
Service call (n8n → API):
```
n8n -> Keycloak: client_credentials(hospital-n8n)   => access_token (cached until 30 s before exp)
n8n -> hospital-api: POST /v1/internal/documents/{id}/parse  Bearer
hospital-api: require_service("svc-n8n") -> handler -> audit(actor="svc-n8n")
```
Insurer callback:
```
tpa-sim/insurer -> hospital-api: POST /v1/insurer-callbacks/status (X-Key-Id, X-Timestamp, X-Idempotency-Key, X-Signature)
hospital-api: verify HMAC + replay window -> idempotency lookup -> handler (doc 06)
```

### 6.4 `/v1/ready` semantics
| Check | Failing result |
|---|---|
| Postgres `SELECT 1` (200 ms budget) | 503 |
| Redis `PING` | 503 |
| JWKS cached or fetchable | 200 with `"auth_jwks_stale": true` if only stale; 503 if none |
| Migrations at head (`alembic_version` equals code head) | 503 `schema_out_of_date` |
Response `{"status":"ok","checks":{"db":"ok","redis":"ok","jwks":"ok","schema":"ok"}}`.

## 7. Config / env vars
| Var | Default | Purpose |
|---|---|---|
| `HOSP_OIDC_ISSUER` | `http://localhost:8080/realms/hospital` | Must equal token `iss` |
| `HOSP_OIDC_JWKS_URL` | `http://keycloak:8080/realms/hospital/protocol/openid-connect/certs` | Container-internal fetch |
| `HOSP_OIDC_AUDIENCE` | `hospital-api` | |
| `HOSP_CORS_ORIGINS` | `http://localhost:3000` | |
| `HOSP_REDIS_URL` | `redis://redis:6379/1` | |
| `HOSP_LOG_LEVEL` | `INFO` | |
| `HOSP_JWT_LEEWAY_S` | `30` | |
| `HOSP_JWKS_TTL_S` | `600` | |
| `HOSP_AUTH_DENY_AUDIT_WINDOW_S` | `60` | |
| `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD` | secrets | |
| `KC_HOSTNAME_URL` | `http://localhost:8080` | |

Issuer mismatch between browser (`localhost:8080`) and container (`keycloak:8080`) is handled by Keycloak `KC_HOSTNAME_URL=http://localhost:8080` and realm `frontendUrl`; the API fetches JWKS via the internal URL but validates `iss` against the public one (PROPOSED: add `extra_hosts: keycloak:host-gateway` so `localhost` and `keycloak` both work in dev).

## 8. Error handling and edge cases
| Situation | Behaviour |
|---|---|
| Expired token | 401 with `WWW-Authenticate: Bearer error="invalid_token", error_description="expired"`; UI refreshes via refresh token. |
| Keycloak down | Existing tokens still verified from cached JWKS (served stale up to 1 h); new logins fail at the UI with a banner; `/v1/ready` reports degraded but stays 200 for `auth_jwks_stale`. |
| Unknown `kid` (key rotation) | Refetch JWKS once (rate limited 1/10 s); still unknown → 401. |
| User with no hospital role | 403 `no_role`; no `app_user` row is created. |
| Role downgrade | Reflected at next token refresh; sensitive actions (sign-off, publish) re-check DB `active` and the live role claim. |
| Role upgrade mid-session | UI calls `/v1/me` after refresh; capabilities re-evaluated. |
| Email change in Keycloak | Upsert by `sub`, never by email; unique `keycloak_sub` index. |
| Two Keycloak users with same email | Second token gets 409 `email_conflict` and an admin alert (email is not unique in DB but is checked on upsert). |
| Clock skew between containers | Leeway 30 s; document NTP assumption; log a warning when `iat` is more than 5 s in the future. |
| Service token on human route | 403 `wrong_service`/`forbidden`. |
| Human token on service route | 403 `forbidden`. |
| Missing HMAC headers on callback | 401 `invalid_signature`. |
| Admin deactivates a user with open sessions | Next request returns 403 `user_disabled` within 30 s (cache TTL); Keycloak session also revoked via admin API call (best effort). |
| Last admin | Cannot be deactivated. |
| CORS preflight | Handled before auth; only allowed origins. |
| Huge `Authorization` header | Reject > 8 KB with 431. |
| Token in query string | Never accepted (SSE uses one-time tickets, doc 07). |
| MFA | Not enabled in MVP; Keycloak OTP policy can be switched on in the realm (PROPOSED extension, no API change). |

### 8.1 Operational runbook (auth)
| Symptom | Likely cause | Fix |
|---|---|---|
| Every call returns 401 `invalid_token` after restart | Realm re-imported, signing keys changed | Restart API or wait 10 min JWKS TTL; log in again |
| `iss` mismatch in logs | Browser used `127.0.0.1` instead of `localhost` | Use `localhost`; or add both to `KC_HOSTNAME` config |
| Desk sees no cases | No `assigned_to` and scope filter bug, or user role cached wrong | Check `/v1/me` roles; `app_user.role` refresh by new login |
| n8n gets 403 `wrong_service` | Client secret belongs to `hospital-crew` | Re-issue secret in `.env`, restart n8n |
| New user gets `no_role` | Realm role not assigned in Keycloak | Assign role; user logs in again |
| Callback 401 `stale_request` | Clock drift > 300 s between containers | Sync host clock; restart Docker Desktop VM |

### 8.2 Error code catalogue owned by this doc
`unauthenticated`, `invalid_token`, `jwks_unavailable`, `forbidden`, `no_role`, `user_disabled`, `wrong_service`, `email_conflict`, `cannot_deactivate_self`, `last_admin`, `invalid_signature`, `stale_request`. Each is added to `claim_contract.errors` only if used across systems (the last two already are); the rest are hospital-private and registered in `hospital/api/app/core/errors.py` with HTTP status and a stable `type` URL `https://claims.local/errors/<code>`.

### 8.3 Security headers and hardening
API responses set `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` on authenticated JSON, `Referrer-Policy: no-referrer`. Request size limit 2 MB for JSON routes (uploads use the documents router limit). Uvicorn runs behind no proxy in dev; `--forwarded-allow-ips` is empty unless `HOSP_BEHIND_PROXY=1`. Dependencies pinned and scanned (`pip-audit` in CI).

## 9. Tests
**Unit**
| Test | Detail |
|---|---|
| JWT validation matrix | wrong aud, wrong iss, expired, not-yet-valid, tampered signature, unknown kid, `alg=none`, `alg=HS256` with public key as secret, missing `sub`, missing `realm_access` |
| Role guard per capability | parametrized over the §3.1 table (role × endpoint) |
| Route protection | every route is guarded or in `PUBLIC` |
| JWKS cache | stale-while-down, refetch on unknown kid, backoff |
| `case_scope` | Desk sees only own + unassigned; Officer/Admin all |
| Capabilities | `/v1/me` output matches table |
| Last-admin / self-deactivate | 409s |
| Log scrubbing | token never present in captured logs |

**Integration (compose)**
| Test | Detail |
|---|---|
| Real Keycloak tokens for each seed user | Hit `/v1/me`, assert roles and capabilities |
| Service-account tokens | `svc-n8n` accepted on `/v1/internal/*`, rejected on human routes; `svc-crew` rejected on n8n-only routes |
| Realm re-import | `make keycloak-reset` then login works with same subs |
| Disabled user | PATCH active=false → 403 within 30 s |
| HMAC | valid signature accepted; tampered body, stale timestamp, wrong key id rejected |
| Issuer mismatch | token issued via `localhost:8080` accepted by the API running in a container |

**Load**: 200 rps `/v1/me` with cached JWKS under 20 ms p95 (locust/k6).

**Security**: tokens signed with a different realm's key rejected; algorithm confusion test; CORS from disallowed origin blocked.

Test matrix (role × route group):
| Route group | desk | officer | admin | svc-n8n | svc-crew | insurer HMAC | anon |
|---|---|---|---|---|---|---|---|
| `/v1/health` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `/v1/me` | ✔ | ✔ | ✔ | ✘ | ✘ | ✘ | ✘ |
| `/v1/cases*` | ✔ (scoped) | ✔ | ✔ read | read | read | ✘ | ✘ |
| `/v1/admin/*` | ✘ | ✘ | ✔ | ✘ | ✘ | ✘ | ✘ |
| `/v1/internal/*` | ✘ | ✘ | ✘ | ✔ | ✔ (crew subset) | ✘ | ✘ |
| `/v1/insurer-callbacks/*` | ✘ | ✘ | ✘ | ✘ | ✘ | ✔ | ✘ |

**Additional negative and abuse tests**
| Test | Expected |
|---|---|
| Token with `aud=account` only | 401 `invalid_token` |
| Token signed by different realm key | 401 |
| Same token replayed after user deactivated | 403 `user_disabled` within 30 s |
| Authorization header 9 KB | 431 |
| Two concurrent first-logins for same sub | exactly one `app_user` row (upsert), no 500 |
| Role claim containing unknown roles only | 403 `no_role` |
| `/v1/admin/users/{id}` with random UUID | 404, not 403 leak of existence for non-admin (non-admin gets 403 first) |
| CORS preflight from `http://evil.local` | no `Access-Control-Allow-Origin` |
| Log capture while failing auth | no token substrings, no `Authorization` |
| Denial audit flood (1000 forbidden calls) | at most 1 audit event per user/route per minute |

**Manual verification script** (`scripts/verify_auth.sh`): obtains tokens for `desk1`, `officer1`, `admin1`, both service clients, then runs a curl matrix against `/v1/me`, `/v1/admin/users`, `/v1/internal/documents/pending-parse` and prints a pass/fail table that must equal the §9 role matrix.

**Coverage targets**: `auth/` ≥ 90 % lines, `core/errors.py` 100 %, routers ≥ 85 %. CI fails below these.

## 10. Acceptance criteria
- [ ] UI login flow yields a token that works against the API for each role.
- [ ] Unauthorised access yields 401/403 with proper problem JSON including `trace_id`.
- [ ] Route-protection test passes; coverage of `auth/` module ≥ 90 %.
- [ ] Realm export re-importable from scratch (`make keycloak-reset`), seed user subs match DB seeds.
- [ ] No token or `Authorization` header appears in any log line (test).
- [ ] `openapi.json` is committed and referenced by the UI type generator.
- [ ] `/v1/me` p95 < 20 ms at 200 rps.
- [ ] Role × route matrix in §9 verified by `scripts/verify_auth.sh` against a running stack.

## 11. Dependencies
Doc 01 (`app_user`, seed mapping), `01-shared-contract/01-05` compose and conventions, `01-01` (HMAC signing, error model), contract `errors.py`. Blocks docs 03–07 and 10. Redis from `04-shared-services/01-infra-redis-minio-clamav-keycloak.md` (Keycloak and Redis compose entries are owned there; this doc owns the realm content).

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/02-api-auth-rbac.md tasks 1-19. Start with the Keycloak realm and compose, then the API skeleton (config, errors, logging), then auth deps, `/v1/me` and admin users, and finish with the tests in section 9 including the route-protection test. Verify with real tokens for each role and for both service accounts. Do not modify contract/ or the DB schema; if a column is missing, stop and update doc 01 section 3.6 first.
