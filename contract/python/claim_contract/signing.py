"""HMAC request signing (01-01 section 4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

from claim_contract.errors import InvalidSignature, SigningError, StaleRequest

__all__ = ["InvalidSignature", "SigningError", "StaleRequest"]

SKEW_SECONDS = 300
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_ts() -> str:
    return datetime.now(UTC).strftime(TS_FORMAT)


def utc_ts(now: datetime | None = None) -> str:
    """RFC 3339 UTC, seconds precision, Z suffix."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime(TS_FORMAT)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise InvalidSignature("malformed timestamp") from exc


def canonical(method: str, target: str, ts: str, idem: str | None, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), target, ts, idem or "", body_hash]).encode()


def sign(secret: bytes, method: str, target: str, ts: str, idem: str | None, body: bytes) -> str:
    mac = hmac.new(secret, canonical(method, target, ts, idem, body), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def verify(
    secrets: dict[str, bytes] | dict[str, list[bytes]],
    key_id: str,
    signature: str,
    method: str,
    target: str,
    ts: str,
    idem: str | None,
    body: bytes,
    now: datetime | None = None,
    skew_seconds: int = SKEW_SECONDS,
) -> None:
    """Raise InvalidSignature / StaleRequest. Order per 01-01 4.2: key, skew, signature.
    `secrets` maps key id to a secret or to a list of secrets (rotation)."""
    entry = secrets.get(key_id)
    if not entry:
        raise InvalidSignature
    candidates = [entry] if isinstance(entry, bytes | bytearray) else list(entry)
    sent = parse_ts(ts)
    now = now or datetime.now(UTC)
    if abs((now - sent).total_seconds()) > skew_seconds:
        raise StaleRequest
    ok = False
    for secret in candidates:  # evaluate all, no early exit, to keep timing flat
        ok |= hmac.compare_digest(sign(bytes(secret), method, target, ts, idem, body), signature)
    if not ok:
        raise InvalidSignature


def build_headers(
    secret: bytes,
    key_id: str,
    method: str,
    target: str,
    body: bytes,
    idem: str | None = None,
    *,
    contract_version: str = "1.1",
    journey_id: str | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Headers for an outgoing signed request; `target` is the exact request target sent on the wire."""
    ts = utc_ts(now)
    headers = {
        "X-Contract-Version": contract_version,
        "X-Key-Id": key_id,
        "X-Timestamp": ts,
        "X-Signature": sign(secret, method, target, ts, idem, body),
    }
    if body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if idem:
        headers["X-Idempotency-Key"] = idem
    if journey_id:
        headers["X-Journey-Id"] = journey_id
    if request_id:
        headers["X-Request-Id"] = request_id
    return headers
