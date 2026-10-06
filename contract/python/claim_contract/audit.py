"""Hash-chained audit events (01-04). `compute_hash` and `redact` are pure; `AuditStore`
abstracts persistence so each side backs it with its own audit_event table."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

GENESIS = "0" * 64


def _default(o: Any) -> str:
    if isinstance(o, Decimal):
        return format(o, "f")
    return str(o)


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def compute_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(body)).encode()).hexdigest()


def event_body(e: dict[str, Any]) -> dict[str, Any]:
    """Hash body. journey_id is deliberately excluded so v1.0/v1.1 events hash identically."""
    keys = (
        "seq",
        "case_id",
        "ts",
        "actor_type",
        "actor_id",
        "event_type",
        "payload",
        "config_versions",
        "model_info",
    )
    return {k: e[k] for k in keys}


# ---- redaction (01-04 section 7) -------------------------------------------------
_PATTERNS = [
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"(?<!\d)(\+91[\s-]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
]
_DROP_KEYS = {"raw_id", "id_number", "address", "full_text", "ocr_text"}
MAX_STR = 2000


def redact(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for pat, repl in _PATTERNS:
            out = pat.sub(repl, out)
        if len(out) > MAX_STR:
            digest = hashlib.sha256(out.encode()).hexdigest()
            out = f"{out[:MAX_STR]}…[truncated {len(out) - MAX_STR}] sha256:{digest}"
        return out
    if isinstance(value, dict):
        res: dict[str, Any] = {}
        for k, v in value.items():
            if k in _DROP_KEYS:
                s = str(v)
                res[f"{k}_sha256"] = hashlib.sha256(s.encode()).hexdigest()
                res[f"{k}_len"] = len(s)
            else:
                res[k] = redact(v)
        return res
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# ---- event registry (01-04 section 8) --------------------------------------------
EVENT_TYPES = frozenset(
    {
        "case.created",
        "doc.uploaded",
        "doc.scanned",
        "doc.parsed",
        "doc.classified",
        "completeness.evaluated",
        "claim.built",
        "human.signoff",
        "claim.submitted",
        "ack.received",
        "query.received",
        "query.draft_generated",
        "query.edited",
        "query.sent",
        "verification.step.completed",
        "decision.recommended",
        "decision.auto_approved",
        "human.approved",
        "human.rejected",
        "escalation.raised",
        "settlement.recorded",
        "config.published",
        "config.retired",
        "outbox.dead",
        "user.login",
        "user.deactivated",
        "permission.denied",
        "case.assigned",
        "case.status_changed",
        "case.updated",
        "doc.deleted",
        "doc.superseded",
        "doc.reclassified",
        "doc.downloaded",
        "doc.split",
        "doc.reparse",
        "doc.quality",
        "parse.agreement",
        "audit.verify.failed",
        "validation.failed",
    }
)


class UnknownEventType(ValueError):
    pass


def assert_known(event_type: str) -> None:
    if event_type not in EVENT_TYPES:
        raise UnknownEventType(event_type)


# ---- verification ----------------------------------------------------------------
@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int = 0
    broken_seq: int | None = None
    reason: str | None = None


class AuditStore(Protocol):
    def events(self, case_id: str) -> Iterable[dict[str, Any]]: ...
    def head(self, case_id: str) -> tuple[int, str] | None: ...


def verify_chain(store: AuditStore, case_id: str) -> VerifyResult:
    prev, expected = GENESIS, 1
    for e in store.events(case_id):
        if e["seq"] != expected:
            return VerifyResult(False, broken_seq=e["seq"], reason="gap_or_reorder")
        if e["prev_hash"] != prev:
            return VerifyResult(False, broken_seq=e["seq"], reason="prev_hash_mismatch")
        if compute_hash(prev, event_body(e)) != e["hash"]:
            return VerifyResult(False, broken_seq=e["seq"], reason="hash_mismatch")
        prev, expected = e["hash"], expected + 1
    head = store.head(case_id)
    if head is not None and head != (expected - 1, prev):
        return VerifyResult(False, reason="head_mismatch")
    return VerifyResult(True, count=expected - 1)


def merkle_root(pairs: list[tuple[str, str]]) -> str:
    """Root over (case_id, last_hash) pairs sorted by case_id."""
    level = [hashlib.sha256(f"{c}:{h}".encode()).hexdigest() for c, h in sorted(pairs)]
    if not level:
        return GENESIS
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256((a + b).encode()).hexdigest()
            for a, b in zip(level[::2], level[1::2], strict=True)
        ]
    return level[0]
