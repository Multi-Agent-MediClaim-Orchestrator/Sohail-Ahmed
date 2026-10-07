"""Pure-ASGI middleware enforcing the contract on hospital↔insurer paths (01-01 §3-5, §10).

Order (first failure wins): body cap → version → required headers → key → timestamp → signature →
rate limit → idempotency → handler.  Body is buffered once (≤ 2 MB) and replayed to the app so the signature is
computed over the raw bytes, never a re-serialised form."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from claim_contract import signing
from claim_contract.errors import ProblemError, problem_response

from .idempotency import (
    IdempotencyStore,
    MemoryIdempotencyStore,
    StoredResponse,
    request_hash,
)

MAX_BODY_BYTES = 2 * 1024 * 1024
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
SUPPORTED_MAJOR = 1


class RateLimiter(Protocol):
    async def hit(self, key_id: str) -> tuple[bool, int, int, int]:
        """Returns (allowed, limit, remaining, reset_seconds)."""
        ...


class MemoryRateLimiter:
    def __init__(self, limit: int = 100, window: int = 60) -> None:
        self.limit, self.window = limit, window
        self._w: dict[str, tuple[int, int]] = {}

    async def hit(self, key_id: str) -> tuple[bool, int, int, int]:
        now = int(time.time())
        win = now // self.window
        w, n = self._w.get(key_id, (win, 0))
        if w != win:
            w, n = win, 0
        n += 1
        self._w[key_id] = (w, n)
        reset = (win + 1) * self.window - now
        return n <= self.limit, self.limit, max(0, self.limit - n), reset


class RedisRateLimiter:
    """Fixed-window counter per key id; fails open when Redis is unavailable."""

    def __init__(self, redis: Any, limit: int = 100, window: int = 60) -> None:
        self.r, self.limit, self.window = redis, limit, window

    async def hit(self, key_id: str) -> tuple[bool, int, int, int]:
        now = int(time.time())
        win = now // self.window
        k = f"rl:{key_id}:{win}"
        try:
            n = await self.r.incr(k)
            if n == 1:
                await self.r.expire(k, self.window + 1)
        except Exception:
            return True, self.limit, self.limit, self.window
        return n <= self.limit, self.limit, max(0, self.limit - n), (win + 1) * self.window - now


@dataclass
class AuthContext:
    key_id: str
    idempotency_key: str | None
    body_hash: str
    request_id: str
    journey_id: str | None
    contract_version: str


SecretsProvider = Callable[[str], Awaitable[list[bytes] | None] | list[bytes] | None]


class ContractAuthMiddleware:
    """``protected_prefixes``: only paths starting with one of these are verified (others pass through).

    ``secrets``: ``key_id -> [current, previous...]`` secrets (rotation) or None for unknown/inactive."""

    def __init__(
        self,
        app: Any,
        *,
        protected_prefixes: Iterable[str],
        secrets: SecretsProvider,
        idempotency: IdempotencyStore | None = None,
        rate_limiter: RateLimiter | None = None,
        supported_minors: tuple[int, ...] = (0, 1),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        skew_seconds: int = signing.SKEW_SECONDS,
        max_body: int = MAX_BODY_BYTES,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        require_idempotency: bool = True,
        server_version: str = "1.1",
    ) -> None:
        self.app = app
        self.prefixes = tuple(protected_prefixes)
        self.secrets = secrets
        self.idem = idempotency if idempotency is not None else MemoryIdempotencyStore()
        self.rl = rate_limiter
        self.supported_minors = supported_minors
        self.now, self.skew, self.max_body = now, skew_seconds, max_body
        self.on_event = on_event or (lambda _n, _d: None)
        self.require_idem = require_idempotency
        self.server_version = server_version

    # -- helpers -------------------------------------------------------------------------------
    async def _secrets_for(self, key_id: str) -> list[bytes] | None:
        res = self.secrets(key_id)
        if inspect.isawaitable(res):
            return await res
        return res

    async def _problem(
        self, send: Any, exc: ProblemError, request_id: str, extra: dict[str, str] | None = None
    ) -> None:
        status, body, headers = problem_response(exc, request_id)
        payload = json.dumps(body).encode()
        hdrs = [
            (b"content-type", b"application/problem+json"),
            (b"content-length", str(len(payload)).encode()),
            (b"x-contract-version", self.server_version.encode()),
            (b"x-request-id", request_id.encode()),
        ]
        for k, v in {**headers, **(extra or {})}.items():
            hdrs.append((k.lower().encode(), v.encode()))
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": payload})

    async def _read_body(self, receive: Any) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                return b"", True
            chunks.append(chunk)
            if not msg.get("more_body", False):
                break
        return b"".join(chunks), False

    # -- ASGI ----------------------------------------------------------------------------------
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefixes):
            await self.app(scope, receive, send)
            return

        hdr = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = hdr.get("x-request-id") or str(uuid.uuid4())
        method = scope["method"].upper()
        raw_path = scope.get("raw_path") or scope["path"].encode()
        qs = scope.get("query_string", b"")
        target = raw_path.decode("latin-1") + (("?" + qs.decode("latin-1")) if qs else "")

        # body cap by declared length first, then while streaming
        try:
            declared = int(hdr.get("content-length", "0") or 0)
        except ValueError:
            declared = 0
        if declared > self.max_body:
            await self._problem(
                send,
                ProblemError("payload_too_large", "body exceeds 2 MB; send documents by URL"),
                request_id,
            )
            return
        body, too_big = await self._read_body(receive)
        if too_big:
            await self._problem(
                send,
                ProblemError("payload_too_large", "body exceeds 2 MB; send documents by URL"),
                request_id,
            )
            return

        try:
            ctx, rl_headers = await self._authenticate(method, target, hdr, body, request_id)
        except ProblemError as exc:
            self.on_event(
                "auth_failed",
                {"code": exc.code, "path": scope["path"], "key_id": hdr.get("x-key-id")},
            )
            await self._problem(send, exc, request_id)
            return

        scope.setdefault("state", {})
        scope["state"]["contract_auth"] = ctx
        scope["state"]["request_id"] = request_id
        scope["state"]["journey_id"] = ctx.journey_id
        scope["state"]["raw_body"] = body

        resp_headers = {
            "x-contract-version": self.server_version,
            "x-request-id": request_id,
            **rl_headers,
        }

        # --- idempotency (mutating methods only) ---
        replay_headers: dict[str, str] = {}
        locked = False
        capture = ctx.idempotency_key is not None and method in MUTATING
        if capture:
            assert ctx.idempotency_key is not None
            rh = request_hash(method, target, body)
            existing = await self.idem.get(ctx.key_id, ctx.idempotency_key)
            if existing is not None:
                if existing.request_hash == rh:
                    await self._send_stored(send, existing, resp_headers)
                    self.on_event("idempotent_replay", {"key_id": ctx.key_id})
                    return
                await self._problem(
                    send,
                    ProblemError(
                        "idempotency_conflict", "idempotency key reused with a different request"
                    ),
                    request_id,
                )
                return
            if not await self.idem.acquire(ctx.key_id, ctx.idempotency_key):
                await self._problem(
                    send,
                    ProblemError(
                        "idempotency_in_progress",
                        "first request still running",
                        headers={"Retry-After": "2"},
                    ),
                    request_id,
                )
                return
            locked = True

        # --- run the app, replaying the buffered body ---
        sent_body = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            msg: dict[str, Any] = await receive()
            return msg

        status_holder: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def capture_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hs = [(k, v) for k, v in message.get("headers", [])]
                existing_names = {k.lower() for k, _ in hs}
                for k, v in resp_headers.items():
                    if k.encode() not in existing_names:
                        hs.append((k.encode(), v.encode()))
                status_holder["headers"] = hs
                message = {**message, "headers": hs}
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(
                scope,
                replay_receive,
                capture_send if capture else _with_headers(send, resp_headers),
            )
            if capture and locked and status_holder.get("status", 500) < 500:
                assert ctx.idempotency_key is not None
                stored_headers = {
                    k.decode(): v.decode("latin-1")
                    for k, v in status_holder.get("headers", [])
                    if k.decode().lower() in ("content-type", "location", "etag")
                }
                await self.idem.put(
                    ctx.key_id,
                    ctx.idempotency_key,
                    StoredResponse(
                        request_hash(method, target, body),
                        status_holder["status"],
                        b"".join(chunks),
                        stored_headers,
                    ),
                )
        finally:
            if locked:
                assert ctx.idempotency_key is not None
                await self.idem.release(ctx.key_id, ctx.idempotency_key)
        _ = replay_headers

    async def _send_stored(self, send: Any, rec: StoredResponse, extra: dict[str, str]) -> None:
        hdrs = [(k.lower().encode(), v.encode()) for k, v in {**rec.headers, **extra}.items()]
        hdrs.append((b"idempotent-replay", b"true"))
        hdrs = [(k, v) for k, v in hdrs if k != b"content-length"] + [
            (b"content-length", str(len(rec.body)).encode())
        ]
        await send({"type": "http.response.start", "status": rec.status, "headers": hdrs})
        await send({"type": "http.response.body", "body": rec.body})

    async def _authenticate(
        self, method: str, target: str, h: dict[str, str], body: bytes, request_id: str
    ) -> tuple[AuthContext, dict[str, str]]:
        ver = h.get("x-contract-version")
        if ver is not None:
            try:
                major, minor = (int(x) for x in ver.split("."))
            except ValueError as exc:
                raise ProblemError(
                    "unsupported_version", f"malformed contract version {ver!r}"
                ) from exc
            if major != SUPPORTED_MAJOR:
                raise ProblemError("unsupported_version", f"major version {major} is not supported")
        key_id, ts, sig = h.get("x-key-id"), h.get("x-timestamp"), h.get("x-signature")
        if not (ver and key_id and ts and sig):
            raise ProblemError(
                "invalid_signature", "signature verification failed"
            )  # never reveal which is missing
        secrets = await self._secrets_for(key_id)
        if not secrets:
            raise ProblemError("invalid_signature", "signature verification failed")
        idem = h.get("x-idempotency-key") or None
        try:
            signing.verify(
                {key_id: secrets},
                key_id,
                sig,
                method,
                target,
                ts,
                idem,
                body,
                now=self.now(),
                skew_seconds=self.skew,
            )
        except signing.StaleRequest as exc:
            raise ProblemError("stale_request", "timestamp outside the allowed window") from exc
        except signing.InvalidSignature as exc:
            raise ProblemError("invalid_signature", "signature verification failed") from exc
        if method in MUTATING and self.require_idem and not idem:
            raise ProblemError(
                "validation_error", "X-Idempotency-Key required for mutating requests"
            )
        if idem is not None:
            try:
                uuid.UUID(idem)
            except ValueError as exc:
                raise ProblemError("validation_error", "X-Idempotency-Key must be a UUID") from exc
        rl_headers: dict[str, str] = {}
        if self.rl is not None:
            allowed, limit, remaining, reset = await self.rl.hit(key_id)
            rl_headers = {
                "x-ratelimit-limit": str(limit),
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(reset),
            }
            if not allowed:
                raise ProblemError(
                    "rate_limited",
                    "too many requests",
                    headers={"Retry-After": str(reset), **rl_headers},
                )
        ctx = AuthContext(
            key_id=key_id,
            idempotency_key=idem,
            body_hash=signing.sha256_hex(body),
            request_id=request_id,
            journey_id=h.get("x-journey-id"),
            contract_version=ver,
        )
        return ctx, rl_headers


def _with_headers(send: Any, extra: dict[str, str]) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def _send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            hs = list(message.get("headers", []))
            names = {k.lower() for k, _ in hs}
            for k, v in extra.items():
                if k.encode() not in names:
                    hs.append((k.encode(), v.encode()))
            message = {**message, "headers": hs}
        await send(message)

    return _send
