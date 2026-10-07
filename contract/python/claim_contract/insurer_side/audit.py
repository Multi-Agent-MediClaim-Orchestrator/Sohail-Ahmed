"""Append-only hash-chained audit log (01-04).

Pure helpers (``canonical_json``, ``compute_hash``, ``redact``, ``merkle_root``) are dependency free;
``append`` / ``verify_chain`` work on a SQLAlchemy ``AsyncSession`` (PostgreSQL in production, SQLite in
unit tests).  ``append`` MUST be called with the same session/transaction that changes business state so
state and audit commit atomically (01-04 §5)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from .audit_events import validate_event

GENESIS = "0" * 64

# --------------------------------------------------------------------------------------------------
# canonical json + hash
# --------------------------------------------------------------------------------------------------


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, datetime):
        return fmt_ts(o)
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "value"):  # enums
        return o.value
    raise TypeError(f"not canonicalisable: {type(o)}")


def fmt_ts(ts: datetime | str) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` (seconds precision, UTC)."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj: Any) -> str:
    """Keys sorted at every level, separators ``(",",":")``, UTF-8 without ASCII escaping, decimals as strings."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_body(
    *,
    seq: int,
    case_id: str | UUID,
    ts: str,
    actor_type: str,
    actor_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    config_versions: Mapping[str, Any] | None,
    model_info: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The hashed body. ``journey_id`` is deliberately excluded so v1.0 and v1.1 events hash identically."""
    return {
        "seq": seq,
        "case_id": str(case_id),
        "ts": ts,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "event_type": event_type,
        "payload": dict(payload),
        "config_versions": dict(config_versions or {}),
        "model_info": dict(model_info) if model_info is not None else None,
    }


def compute_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(body))


# --------------------------------------------------------------------------------------------------
# redaction (01-04 §7)
# --------------------------------------------------------------------------------------------------
AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
PHONE = re.compile(r"(?<![\w.])(?:\+91[\s-]?)?[6-9]\d{9}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PII_PATTERNS = (AADHAAR, PAN, PHONE, EMAIL)
_DROP_KEYS = {"raw_id", "id_number", "address", "full_text", "ocr_text"}
_NAME_KEYS = {"patient_name", "full_name", "patient_full_name", "member_name"}
MAX_STR = 2000


def _hash8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


_SAFE_TOKEN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{64}\b"
)


def redact_string(s: str) -> str:
    # UUIDs and sha256 hashes are allowed content (01-04 §7) and must not trip the digit patterns
    safe: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        safe.append(m.group(0))
        return f"\x00{len(safe) - 1}\x00"

    s = _SAFE_TOKEN.sub(_stash, s)
    s = AADHAAR.sub("[AADHAAR]", s)
    s = PAN.sub("[PAN]", s)
    s = EMAIL.sub("[EMAIL]", s)
    s = PHONE.sub("[PHONE]", s)
    if safe:
        s = re.sub(r"\x00(\d+)\x00", lambda m: safe[int(m.group(1))], s)
    if len(s) > MAX_STR:
        s = f"{s[:MAX_STR]}…[truncated {len(s) - MAX_STR}] sha256:{hashlib.sha256(s.encode()).hexdigest()}"
    return s


def redact(obj: Any) -> Any:
    """Recursively redact a JSON-shaped payload before hashing/storing."""
    if isinstance(obj, str):
        return redact_string(obj)
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k)
            if key in _DROP_KEYS:
                raw = v if isinstance(v, str) else canonical_json(v)
                out[f"{key}_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                out[f"{key}_len"] = len(raw)
            elif key in _NAME_KEYS and isinstance(v, str):
                out[key] = f"patient:{_hash8(v)}"
            else:
                out[key] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def contains_pii(s: str) -> bool:
    s = _SAFE_TOKEN.sub("", s)
    return any(p.search(s) for p in PII_PATTERNS)


# --------------------------------------------------------------------------------------------------
# merkle anchor
# --------------------------------------------------------------------------------------------------
def merkle_root(pairs: list[tuple[str, str]]) -> str:
    """Merkle root over ``(case_id, last_hash)`` sorted by case_id."""
    if not pairs:
        return GENESIS
    level = [sha256_hex(f"{cid}:{h}") for cid, h in sorted(pairs, key=lambda p: p[0])]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256_hex(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


# --------------------------------------------------------------------------------------------------
# DB layer
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AuditTables:
    event: str = "audit.audit_event"
    head: str = "audit.case_audit_head"
    anchor: str = "audit.audit_anchor"

    @classmethod
    def flat(cls) -> AuditTables:  # SQLite / hospital default schema
        return cls("audit_event", "case_audit_head", "audit_anchor")


@dataclass(frozen=True)
class Ok:
    count: int
    ok: bool = True


@dataclass(frozen=True)
class Broken:
    seq: int | None
    reason: str  # gap_or_reorder | prev_hash_mismatch | hash_mismatch | head_mismatch
    ok: bool = False


VerifyResult = Ok | Broken


def _is_pg(session: Any) -> bool:
    return session.bind.dialect.name == "postgresql" if session.bind is not None else True


def _loads(v: Any) -> Any:
    if isinstance(v, (str, bytes)):
        return json.loads(v)
    return v


def new_event_id() -> UUID:
    try:
        from uuid_utils import uuid7

        return UUID(str(uuid7()))
    except ImportError:  # pragma: no cover
        import uuid

        return uuid.uuid4()


async def append(
    session: Any,
    case_id: UUID,
    actor_type: str,
    actor_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    config_versions: Mapping[str, Any] | None = None,
    model_info: Mapping[str, Any] | None = None,
    journey_id: UUID | None = None,
    *,
    tables: AuditTables | None = None,
    now: datetime | None = None,
    redis: Any | None = None,
) -> int:
    """Append one event inside the caller's transaction and return its ``seq``.

    Per-case appends serialise on ``SELECT ... FOR UPDATE`` of the head row; different cases never block."""
    from sqlalchemy import text

    tables = tables or AuditTables()
    pg = _is_pg(session)
    actor_type = getattr(actor_type, "value", actor_type)
    redacted = redact(dict(payload or {}))
    validate_event(event_type, actor_type, redacted)

    await session.execute(
        text(
            f"INSERT INTO {tables.head} (case_id, last_seq, last_hash) VALUES (:c, 0, :z) ON CONFLICT (case_id) DO NOTHING"
        ),
        {"c": str(case_id), "z": GENESIS},
    )
    lock = " FOR UPDATE" if pg else ""
    row = (
        await session.execute(
            text(f"SELECT last_seq, last_hash FROM {tables.head} WHERE case_id = :c{lock}"),
            {"c": str(case_id)},
        )
    ).one()
    seq = int(row.last_seq) + 1
    ts_dt = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    ts = fmt_ts(ts_dt)
    body = hash_body(
        seq=seq,
        case_id=case_id,
        ts=ts,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        payload=redacted,
        config_versions=config_versions,
        model_info=model_info,
    )
    h = compute_hash(row.last_hash, body)
    # ensure JSON round-trip stable: store exactly what was hashed
    payload_json = canonical_json(redacted)
    cfg_json = canonical_json(dict(config_versions or {}))
    model_json = canonical_json(dict(model_info)) if model_info is not None else None

    def cast(n: str) -> str:
        return f"CAST(:{n} AS JSONB)" if pg else f":{n}"

    ts_param: Any = ts_dt if pg else ts
    await session.execute(
        text(
            f"INSERT INTO {tables.event} (id, case_id, seq, ts, actor_type, actor_id, event_type, payload, "
            f"config_versions, model_info, journey_id, prev_hash, hash) VALUES (:id, :case_id, :seq, :ts, "
            f":actor_type, :actor_id, :event_type, {cast('payload')}, {cast('cfg')}, "
            f"{cast('model') if model_json is not None else 'NULL'}, :journey_id, :prev_hash, :hash)"
        ),
        {
            "id": str(new_event_id()) if not pg else new_event_id(),
            "case_id": str(case_id) if not pg else case_id,
            "seq": seq,
            "ts": ts_param,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "event_type": event_type,
            "payload": payload_json,
            "cfg": cfg_json,
            **({"model": model_json} if model_json is not None else {}),
            "journey_id": (journey_id if pg else (str(journey_id) if journey_id else None)),
            "prev_hash": row.last_hash,
            "hash": h,
        },
    )
    await session.execute(
        text(f"UPDATE {tables.head} SET last_seq = :s, last_hash = :h WHERE case_id = :c"),
        {"s": seq, "h": h, "c": str(case_id) if not pg else case_id},
    )
    if redis is not None:  # notification only; failure never breaks the business transaction
        try:
            await redis.publish(
                "audit.appended",
                json.dumps({"case_id": str(case_id), "seq": seq, "event_type": event_type}),
            )
        except Exception:  # pragma: no cover
            pass
    return seq


async def verify_chain(
    session: Any, case_id: UUID, *, tables: AuditTables | None = None
) -> VerifyResult:
    from sqlalchemy import text

    tables = tables or AuditTables()
    pg = _is_pg(session)
    cid: Any = case_id if pg else str(case_id)
    rows = (
        await session.execute(
            text(
                f"SELECT seq, ts, actor_type, actor_id, event_type, payload, config_versions, model_info, "
                f"prev_hash, hash FROM {tables.event} WHERE case_id = :c ORDER BY seq"
            ),
            {"c": cid},
        )
    ).all()
    prev = GENESIS
    expected = 1
    for e in rows:
        if e.seq != expected:
            return Broken(int(e.seq), "gap_or_reorder")
        if e.prev_hash != prev:
            return Broken(int(e.seq), "prev_hash_mismatch")
        body = hash_body(
            seq=int(e.seq),
            case_id=case_id,
            ts=fmt_ts(e.ts),
            actor_type=e.actor_type,
            actor_id=e.actor_id,
            event_type=e.event_type,
            payload=_loads(e.payload),
            config_versions=_loads(e.config_versions),
            model_info=_loads(e.model_info) if e.model_info is not None else None,
        )
        if compute_hash(prev, body) != e.hash:
            return Broken(int(e.seq), "hash_mismatch")
        prev = e.hash
        expected += 1
    head = (
        await session.execute(
            text(f"SELECT last_seq, last_hash FROM {tables.head} WHERE case_id = :c"), {"c": cid}
        )
    ).one_or_none()
    if head is not None and (head.last_seq != expected - 1 or head.last_hash != prev):
        return Broken(None, "head_mismatch")
    if head is None and rows:
        return Broken(None, "head_mismatch")
    return Ok(expected - 1)


async def read_events(
    session: Any,
    case_id: UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
    tables: AuditTables | None = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    tables = tables or AuditTables()
    pg = _is_pg(session)
    rows = (
        await session.execute(
            text(
                f"SELECT seq, ts, actor_type, actor_id, event_type, payload, config_versions, model_info, hash "
                f"FROM {tables.event} WHERE case_id = :c AND seq > :a ORDER BY seq LIMIT :l"
            ),
            {"c": case_id if pg else str(case_id), "a": after_seq, "l": limit},
        )
    ).all()
    return [
        {
            "seq": int(r.seq),
            "ts": fmt_ts(r.ts),
            "actor_type": r.actor_type,
            "actor_id": r.actor_id,
            "event_type": r.event_type,
            "payload": _loads(r.payload),
            "config_versions": _loads(r.config_versions),
            "model_info": _loads(r.model_info) if r.model_info is not None else None,
            "hash": r.hash,
        }
        for r in rows
    ]


async def compute_anchor(
    session: Any, *, tables: AuditTables | None = None, touched_since: datetime | None = None
) -> tuple[str, int]:
    """Merkle root over every case head (optionally only cases touched since a timestamp). Returns (root, cases)."""
    from sqlalchemy import text

    tables = tables or AuditTables()
    rows = (await session.execute(text(f"SELECT case_id, last_hash FROM {tables.head}"))).all()
    pairs = [(str(r.case_id), r.last_hash) for r in rows]
    return merkle_root(pairs), len(pairs)
