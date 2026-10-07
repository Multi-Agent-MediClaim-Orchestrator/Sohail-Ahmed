"""Append-only hash-chained audit events (01-04). `append` runs in the caller's transaction so
state changes and their audit rows commit atomically."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from claim_contract import audit as ca
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SYSTEM_CASE = uuid.UUID(int=0)  # chain for events that belong to no case (users, config, denials)


async def append(
    session: AsyncSession,
    case_id: uuid.UUID | str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    actor_type: str = "system",
    actor_id: str = "hospital-api",
    config_versions: dict[str, Any] | None = None,
    model_info: dict[str, Any] | None = None,
    journey_id: str | None = None,
) -> int:
    ca.assert_known(event_type)
    case_id = uuid.UUID(str(case_id))
    clean = ca.redact(payload or {})
    # One lock order everywhere: case row first, audit head second. Status transitions already update the case row and
    # then append; without this, two requests on one case could take the two locks in opposite orders and deadlock
    # (the UI's three parallel uploads did). NO KEY UPDATE does not block foreign-key checks from child inserts.
    await session.execute(
        text("SELECT 1 FROM claim_case WHERE id = :c FOR NO KEY UPDATE"), {"c": case_id}
    )
    await session.execute(
        text(
            "INSERT INTO case_audit_head (case_id, last_seq, last_hash) VALUES (:c, 0, :g) "
            "ON CONFLICT (case_id) DO NOTHING"
        ),
        {"c": case_id, "g": ca.GENESIS},
    )
    head = (
        await session.execute(
            text("SELECT last_seq, last_hash FROM case_audit_head WHERE case_id = :c FOR UPDATE"),
            {"c": case_id},
        )
    ).one()
    seq = head.last_seq + 1
    ts = datetime.now(UTC).replace(microsecond=0)
    body = {
        "seq": seq,
        "case_id": str(case_id),
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "event_type": event_type,
        "payload": clean,
        "config_versions": config_versions or {},
        "model_info": model_info,
    }
    h = ca.compute_hash(head.last_hash, ca.event_body(body))
    await session.execute(
        text(
            "INSERT INTO audit_event (id, seq, case_id, ts, actor_type, actor_id, event_type, payload, "
            "config_versions, model_info, journey_id, prev_hash, hash) VALUES (:id, :seq, :c, :ts, "
            "CAST(:at AS audit_actor_type), :aid, :et, CAST(:p AS jsonb), CAST(:cv AS jsonb), "
            "CAST(:mi AS jsonb), :j, :ph, :h)"
        ),
        {
            "id": uuid.UUID(str(uuid_utils.uuid7())),
            "seq": seq,
            "c": case_id,
            "ts": ts,
            "at": actor_type,
            "aid": actor_id,
            "et": event_type,
            "p": json.dumps(clean, default=str),
            "cv": json.dumps(config_versions or {}),
            "mi": json.dumps(model_info) if model_info else None,
            "j": journey_id,
            "ph": head.last_hash,
            "h": h,
        },
    )
    await session.execute(
        text(
            "UPDATE case_audit_head SET last_seq = :s, last_hash = :h, updated_at = now() WHERE case_id = :c"
        ),
        {"s": seq, "h": h, "c": case_id},
    )
    return seq


async def verify(session: AsyncSession, case_id: uuid.UUID | str) -> ca.VerifyResult:
    """Recompute the chain for a case from the database."""
    cid = uuid.UUID(str(case_id))
    rows = (
        (
            await session.execute(
                text(
                    "SELECT seq, case_id, ts, actor_type::text AS actor_type, actor_id, event_type, payload, "
                    "config_versions, model_info, prev_hash, hash FROM audit_event WHERE case_id = :c ORDER BY seq"
                ),
                {"c": cid},
            )
        )
        .mappings()
        .all()
    )
    events = [
        {
            **r,
            "case_id": str(r["case_id"]),
            "ts": r["ts"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for r in rows
    ]
    head = (
        await session.execute(
            text("SELECT last_seq, last_hash FROM case_audit_head WHERE case_id=:c"), {"c": cid}
        )
    ).one_or_none()

    class _Store:
        def events(self, case_id: str) -> list[dict[str, Any]]:
            return events

        def head(self, case_id: str) -> tuple[int, str] | None:
            return (head.last_seq, head.last_hash) if head else None

    return ca.verify_chain(_Store(), str(cid))
