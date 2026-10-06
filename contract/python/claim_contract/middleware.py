"""Pure-ASGI contract middleware (01-01 §3-5, §9-10): version check, body-size cap, HMAC verification
over the raw body, rate limiting, idempotency and request/journey-id propagation.

Applies only to paths under `protect_prefixes`; health/contract endpoints stay open."""

from __future__ import annotations

import contextvars
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from claim_contract import signing
from claim_contract.errors import ContractError
from claim_contract.idempotency import IdempotencyStore, StoredResponse, request_hash

_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SUPPORTED_MAJOR = "1"
MAX_BODY = 2 * 1024 * 1024
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
journey_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "journey_id", default=None
)
key_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("key_id", default=None)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class RateLimiter:
    """Fixed-window counter per key id (100 req/min by default). Redis-backed when given a client
    (`rl:{ns}:{key_id}:{minute}`), in-process otherwise."""

    def __init__(self, namespace: str, limit: int = 100, redis: Any = None) -> None:
        self.ns, self.limit, self.redis = namespace, limit, redis
        self._mem: dict[str, int] = {}

    async def hit(self, key_id: str, now: float | None = None) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_epoch)."""
        minute = int((now or time.time()) // 60)
        k = f"rl:{self.ns}:{key_id}:{minute}"
        if self.redis is not None:
            n = int(await self.redis.incr(k))
            if n == 1:
                await self.redis.expire(k, 120)
        else:
            n = self._mem[k] = self._mem.get(k, 0) + 1
        return n <= self.limit, max(self.limit - n, 0), (minute + 1) * 60


@dataclass
class ContractConfig:
    secrets: dict[str, bytes]
    store: IdempotencyStore | None = None
    protect_prefixes: tuple[str, ...] = ("/v1/",)
    open_paths: tuple[str, ...] = ("/v1/health", "/v1/contract")
    rate_limiter: RateLimiter | None = None
    now: Callable[[], Any] | None = None  # injectable clock for tests
    contract_version: str = "1.1"
    verify_signatures: bool = True
    route_match: Callable[[Scope], bool] | None = (
        None  # False => pass through (router answers 404/405)
    )


def _problem_response(
    err: ContractError, request_id: str, extra: dict[str, str] | None = None
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    pd = err.problem()
    pd.trace_id = pd.trace_id or request_id
    body = pd.model_dump_json().encode()
    headers = [
        (b"content-type", b"application/problem+json"),
        (b"x-request-id", request_id.encode()),
    ]
    headers += [(k.lower().encode(), v.encode()) for k, v in (extra or {}).items()]
    return pd.status, headers, body


class ContractMiddleware:
    def __init__(self, app: Any, config: ContractConfig) -> None:
        self.app, self.cfg = app, config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self._protected(scope["path"])
            or scope["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or (self.cfg.route_match is not None and not self.cfg.route_match(scope))
        ):
            # other methods go straight to the app, which answers 405 for routes it does not serve
            await self.app(scope, receive, send)
            return
        hdr = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        rid = hdr["x-request-id"] if "x-request-id" in hdr else str(uuid.uuid4())
        extra: dict[str, str] = {}
        try:
            if not _RID.match(rid):
                rid = str(uuid.uuid4())
                raise ContractError(
                    "bad_request", "X-Request-Id must be 1-64 chars of A-Za-z0-9._-"
                )
            jid = hdr.get("x-journey-id")  # present-but-empty is malformed, not absent
            if jid is not None:
                try:
                    uuid.UUID(jid)
                except ValueError:
                    raise ContractError("bad_request", "X-Journey-Id must be a UUID") from None
            request_id_var.set(rid)
            journey_id_var.set(jid)
            body = await self._read_body(receive)
            key_id = self._authenticate(scope, hdr, body)
            key_id_var.set(key_id)
            if self.cfg.rate_limiter is not None:
                ok, remaining, reset = await self.cfg.rate_limiter.hit(key_id)
                extra = {
                    "X-RateLimit-Limit": str(self.cfg.rate_limiter.limit),
                    "X-RateLimit-Remaining": str(remaining),
                    "X-RateLimit-Reset": str(reset),
                }
                if not ok:
                    extra["Retry-After"] = str(max(reset - int(time.time()), 1))
                    raise ContractError("rate_limited", "rate limit exceeded")
            await self._dispatch(scope, send, hdr, rid, key_id, body, extra)
        except ContractError as err:
            status, headers, payload = _problem_response(err, rid, extra)
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": payload})

    def _protected(self, path: str) -> bool:
        return path.startswith(self.cfg.protect_prefixes) and path not in self.cfg.open_paths

    async def _read_body(self, receive: Receive) -> bytes:
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY:
                raise ContractError("payload_too_large", "body exceeds 2 MB; send documents by URL")
            chunks.append(chunk)
            if not msg.get("more_body"):
                break
        return b"".join(chunks)

    def _authenticate(self, scope: Scope, hdr: dict[str, str], body: bytes) -> str:
        ver = hdr.get("x-contract-version", "")
        if ver.split(".")[0] != SUPPORTED_MAJOR:
            raise ContractError("unsupported_version", f"unsupported contract version {ver!r}")
        key_id = hdr.get("x-key-id", "")
        required = ("x-key-id", "x-timestamp", "x-signature")
        if any(not hdr.get(h) for h in required):
            raise ContractError("invalid_signature", "signature verification failed")
        if scope["method"] in MUTATING:
            idem_hdr = hdr.get("x-idempotency-key", "")
            try:
                uuid.UUID(idem_hdr)
            except ValueError:
                raise ContractError("bad_request", "X-Idempotency-Key (UUID) is required") from None
        if not self.cfg.verify_signatures:  # presence-only mode for schema tests (values unchecked)
            return key_id
        method = scope["method"]
        target = scope["raw_path"].decode() if scope.get("raw_path") else scope["path"]
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode()
        idem = hdr.get("x-idempotency-key") if method in MUTATING else None
        signing.verify(
            self.cfg.secrets,
            key_id,
            hdr["x-signature"],
            method,
            target,
            hdr["x-timestamp"],
            idem,
            body,
            now=self.cfg.now() if self.cfg.now else None,
        )
        return key_id

    async def _dispatch(
        self,
        scope: Scope,
        send: Send,
        hdr: dict[str, str],
        rid: str,
        key_id: str,
        body: bytes,
        extra: dict[str, str],
    ) -> None:
        sent_body = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal sent_body
            if sent_body:
                return {"type": "http.disconnect"}
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}

        base_headers = [
            (b"x-contract-version", self.cfg.contract_version.encode()),
            (b"x-request-id", rid.encode()),
        ]
        base_headers += [(k.lower().encode(), v.encode()) for k, v in extra.items()]
        idem = hdr.get("x-idempotency-key")
        method = scope["method"]
        if self.cfg.store is None or method not in MUTATING or not idem:

            async def plain_send(m: dict[str, Any]) -> None:
                if m["type"] == "http.response.start":
                    m = {**m, "headers": list(m.get("headers", [])) + base_headers}
                await send(m)

            await self.app(scope, replay_receive, plain_send)
            return

        target = scope.get("raw_path", scope["path"].encode()).decode()
        rh = request_hash(method, target, body)
        k = f"{key_id}:{idem}"
        store = self.cfg.store
        rec = await store.get(k)
        if rec is not None:
            if rec.request_hash != rh:
                raise ContractError(
                    "idempotency_conflict", "same key used with a different request"
                )
            await self._emit(send, rec, base_headers, replayed=True)
            return
        if not await store.acquire_lock(k):
            raise ContractError("idempotency_in_progress", "first request still running")
        try:
            captured: dict[str, Any] = {"status": 500, "headers": [], "body": b""}

            async def capture_send(m: dict[str, Any]) -> None:
                if m["type"] == "http.response.start":
                    captured["status"], captured["headers"] = (
                        m["status"],
                        list(m.get("headers", [])),
                    )
                elif m["type"] == "http.response.body":
                    captured["body"] += m.get("body", b"")

            await self.app(scope, replay_receive, capture_send)
            hdrs = {k_.decode(): v.decode() for k_, v in captured["headers"]}
            rec = StoredResponse(rh, captured["status"], captured["body"], hdrs)
            if rec.status < 500:  # 5xx is never stored: the client retries with the same key
                await store.put(k, rec)
            await self._emit(send, rec, base_headers, replayed=False)
        finally:
            await store.release_lock(k)

    @staticmethod
    async def _emit(
        send: Send, rec: StoredResponse, base: Iterable[tuple[bytes, bytes]], replayed: bool
    ) -> None:
        skip = {"content-length", "x-request-id", "x-contract-version"}
        headers = [
            (k.lower().encode(), v.encode())
            for k, v in rec.headers.items()
            if k.lower() not in skip
        ]
        headers += list(base) + [(b"content-length", str(len(rec.body)).encode())]
        if replayed:
            headers.append((b"idempotent-replay", b"true"))
        await send({"type": "http.response.start", "status": rec.status, "headers": headers})
        await send({"type": "http.response.body", "body": rec.body})


def health_payload(version: str = "1.1.0") -> dict[str, str]:
    from datetime import UTC, datetime

    return {
        "status": "ok",
        "version": version,
        "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def contract_payload() -> dict[str, Any]:
    return {"supported": ["1.0", "1.1"], "default": "1.1", "deprecated": []}


__all__ = [
    "starlette_route_matcher",
    "ContractConfig",
    "ContractMiddleware",
    "RateLimiter",
    "contract_payload",
    "health_payload",
    "journey_id_var",
    "key_id_var",
    "request_id_var",
]


def starlette_route_matcher(routes: list[Any]) -> Callable[[Scope], bool]:
    """route_match helper: True only when some route fully matches path and method."""
    from starlette.routing import Match

    def match(scope: Scope) -> bool:
        return any(r.matches(scope)[0] == Match.FULL for r in routes)

    return match
