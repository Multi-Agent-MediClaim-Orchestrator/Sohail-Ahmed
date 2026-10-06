"""HMAC request signing (01-01 section 4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

from claim_contract.errors import InvalidSignature, StaleRequest

SKEW_SECONDS = 300
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_ts() -> str:
    return datetime.now(UTC).strftime(TS_FORMAT)


def canonical(method: str, target: str, ts: str, idem: str | None, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), target, ts, idem or "", body_hash]).encode()


def sign(secret: bytes, method: str, target: str, ts: str, idem: str | None, body: bytes) -> str:
    mac = hmac.new(secret, canonical(method, target, ts, idem, body), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def verify(
    secrets: dict[str, bytes],
    key_id: str,
    signature: str,
    method: str,
    target: str,
    ts: str,
    idem: str | None,
    body: bytes,
    now: datetime | None = None,
) -> None:
    """Raise InvalidSignature / StaleRequest. Order per 01-01 4.2: key, skew, signature."""
    secret = secrets.get(key_id)
    if secret is None:
        raise InvalidSignature
    try:
        sent = datetime.strptime(ts, TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise InvalidSignature from None
    now = now or datetime.now(UTC)
    if abs((now - sent).total_seconds()) > SKEW_SECONDS:
        raise StaleRequest
    expected = sign(secret, method, target, ts, idem, body)
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature
