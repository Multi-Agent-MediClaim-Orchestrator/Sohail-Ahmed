"""Insurer -> hospital callbacks (doc 06 §5.5, §5.7; contract 01-01 §6.2): dedupe, ordering, state mapping."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from claim_contract import models as cm
from claim_contract.enums import HospitalCaseStatus as H
from claim_contract.enums import InsurerCaseStatus
from claim_contract.transitions import (
    HOSPITAL_TRANSITIONS,
    HOSPITAL_VISIBLE,
    InvalidTransition,
    assert_hospital,
)
from sqlalchemy import text

from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, transitions

DECISION_STATUS = {"approve": "approved", "partial": "partially_approved", "reject": "rejected"}


class Replay(Exception):
    """Raised inside a handler when the callback was already processed; carries the stored response."""

    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body


async def lock_case(uow: UoW, claim_ref: str) -> Any:
    row = (
        await uow.session.execute(
            text(
                "SELECT c.*, c.status::text AS status_t FROM claim_case c WHERE c.claim_ref=:r FOR NO KEY UPDATE"
            ),
            {"r": claim_ref},
        )
    ).first()
    if row is None:
        raise ApiError("unknown_claim", f"unknown claim {claim_ref}")
    return row


async def record(
    uow: UoW, claim_ref: str, kind: str, sequence: int, idem: str, body: dict[str, Any]
) -> tuple[bool, int, Any]:
    """Insert into inbound_callback. Returns (is_new, stored_status, stored_body). Same sequence with a different
    body is an idempotency conflict (409)."""
    s = uow.session
    ins = (
        await s.execute(
            text(
                "INSERT INTO inbound_callback (id, claim_ref, kind, sequence, idempotency_key, body) VALUES (uuid_generate_v7(), :r, :k, :n, "
                ":i, CAST(:b AS jsonb)) ON CONFLICT DO NOTHING RETURNING id"
            ),
            {
                "r": claim_ref,
                "k": kind,
                "n": sequence,
                "i": uuid.UUID(idem),
                "b": json.dumps(body, default=str),
            },
        )
    ).first()
    if ins is not None:
        return True, 204, None
    prior = (
        await s.execute(
            text(
                "SELECT body, response_status, response_body FROM inbound_callback WHERE (claim_ref=:r AND kind=:k AND sequence=:n) "
                "OR idempotency_key=:i LIMIT 1"
            ),
            {"r": claim_ref, "k": kind, "n": sequence, "i": uuid.UUID(idem)},
        )
    ).first()
    if prior is None or json.loads(json.dumps(prior.body, sort_keys=True)) != json.loads(
        json.dumps(body, default=str, sort_keys=True)
    ):
        raise ApiError(
            "idempotency_conflict", "this sequence was already received with a different body"
        )
    return False, prior.response_status, prior.response_body


async def last_sequence(uow: UoW, claim_ref: str) -> int:
    """Highest sequence seen for the claim; the acknowledgement of the submission counts as sequence 1."""
    row = (
        await uow.session.execute(
            text(
                "SELECT GREATEST(COALESCE((SELECT max(sequence) FROM inbound_callback WHERE claim_ref=:r), 0), "
                "CASE WHEN c.acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) FROM claim_case c WHERE c.claim_ref=:r"
            ),
            {"r": claim_ref},
        )
    ).scalar()
    return int(row or 0)


async def note_gap(uow: UoW, case: Any, claim_ref: str, sequence: int, prev_last: int) -> None:
    if prev_last and sequence > prev_last + 1:
        await audit.append(
            uow.session,
            case.id,
            "callback.sequence_gap",
            {"last": prev_last, "incoming": sequence},
            actor_type="external",
            actor_id="insurer",
        )


async def apply_if_valid(
    uow: UoW, case: Any, target: str, hub: Any, note: str | None = None
) -> bool:
    """Apply a forward transition; record (and acknowledge) anything backward or invalid. Returns True if applied."""
    current = (
        await uow.session.execute(
            text("SELECT status::text FROM claim_case WHERE id=:i"), {"i": case.id}
        )
    ).scalar()
    if current == target:
        return False
    if (
        current == "submitted"
        and target not in ("acknowledged", "submitted")
        and H(target) not in HOSPITAL_TRANSITIONS[H.SUBMITTED]
    ):
        await transitions.transition(
            uow, case.id, "acknowledged", None, reason="implied by insurer callback", hub=hub
        )
        current = "acknowledged"
    try:
        assert_hospital(H(current), H(target))
    except InvalidTransition:
        await audit.append(
            uow.session,
            case.id,
            "callback.ignored_invalid_transition",
            {"from": current, "to": target},
            actor_type="external",
            actor_id="insurer",
        )
        return False
    await transitions.transition(
        uow, case.id, target, None, reason=note or "insurer callback", hub=hub
    )
    return True


async def handle_status(uow: UoW, upd: cm.StatusUpdate, idem: str, hub: Any) -> tuple[int, Any]:
    case = await lock_case(uow, upd.claim_ref)
    prev = await last_sequence(uow, upd.claim_ref)
    is_new, st, body = await record(
        uow, upd.claim_ref, "status", upd.sequence, idem, upd.model_dump(mode="json")
    )
    if not is_new:
        return st, body
    await note_gap(uow, case, upd.claim_ref, upd.sequence, prev)
    stale = await _newer_status_exists(
        uow, upd.claim_ref, upd.sequence
    )  # a newer status already arrived
    if not stale:
        await uow.session.execute(
            text(
                "UPDATE claim_case SET insurer_claim_no=COALESCE(insurer_claim_no, :n), insurer_status=:s, "
                "acknowledged_at=COALESCE(acknowledged_at, now()) WHERE id=:i"
            ),
            {"n": upd.insurer_claim_no, "s": upd.status.value, "i": case.id},
        )
        target = HOSPITAL_VISIBLE[InsurerCaseStatus(upd.status.value)].value
        await apply_if_valid(uow, case, target, hub, upd.note)
    await audit.append(
        uow.session,
        case.id,
        "callback.status",
        {"insurer_status": upd.status.value, "seq": upd.sequence, "stale": stale},
        actor_type="external",
        actor_id="insurer",
    )
    return 204, None


async def _newer_status_exists(uow: UoW, claim_ref: str, seq: int) -> bool:
    return bool(
        (
            await uow.session.execute(
                text(
                    "SELECT 1 FROM inbound_callback WHERE claim_ref=:r AND kind='status' AND sequence > :n LIMIT 1"
                ),
                {"r": claim_ref, "n": seq},
            )
        ).first()
    )


async def handle_decision(
    uow: UoW,
    claim_ref: str,
    seq: int,
    decision: cm.Decision,
    idem: str,
    raw: dict[str, Any],
    hub: Any,
) -> tuple[int, Any]:
    case = await lock_case(uow, claim_ref)
    prev = await last_sequence(uow, claim_ref)
    is_new, st, body = await record(uow, claim_ref, "decisions", seq, idem, raw)
    if not is_new:
        return st, body
    await note_gap(uow, case, claim_ref, seq, prev)
    s = uow.session
    approved = decision.approved_amount.amount
    claimed: Decimal | None = case.claimed_amount
    short = (claimed - approved) if claimed is not None else None
    if claimed is not None and approved > claimed:
        await audit.append(
            s,
            case.id,
            "decision.exceeds_claim",
            {"approved": str(approved), "claimed": str(claimed)},
            actor_type="external",
            actor_id="insurer",
        )
    await s.execute(
        text(
            "UPDATE claim_case SET decision=CAST(:d AS jsonb), approved_amount=:a, short_pay_amount=:sp WHERE id=:i"
        ),
        {
            "d": json.dumps(decision.model_dump(mode="json")),
            "a": approved,
            "sp": short,
            "i": case.id,
        },
    )
    target = DECISION_STATUS.get(decision.outcome.value)
    if target:
        await apply_if_valid(uow, case, target, hub, "insurer decision")
    await audit.append(
        s,
        case.id,
        "callback.decision",
        {
            "outcome": decision.outcome.value,
            "approved": str(approved),
            "seq": seq,
            "short_pay": str(short) if short is not None else None,
        },
        actor_type="external",
        actor_id="insurer",
    )

    async def pub() -> None:
        await hub.publish("decision.received", str(case.id), {"outcome": decision.outcome.value})

    uow.after_commit(pub)
    return 204, None


async def handle_settlement(
    uow: UoW,
    claim_ref: str,
    seq: int,
    st_: cm.SettlementNotice,
    idem: str,
    raw: dict[str, Any],
    hub: Any,
) -> tuple[int, Any]:
    case = await lock_case(uow, claim_ref)
    prev = await last_sequence(uow, claim_ref)
    is_new, st, body = await record(uow, claim_ref, "settlements", seq, idem, raw)
    if not is_new:
        return st, body
    await note_gap(uow, case, claim_ref, seq, prev)
    s = uow.session
    ins = (
        await s.execute(
            text(
                "INSERT INTO settlement (id, case_id, settlement_id, utr, amount, tds, mode, paid_on, raw) VALUES (uuid_generate_v7(), :c, :sid, "
                ":u, :a, :t, :m, :p, CAST(:r AS jsonb)) ON CONFLICT ON CONSTRAINT uq_settlement_case_utr DO NOTHING RETURNING id"
            ),
            {
                "c": case.id,
                "sid": st_.settlement_id,
                "u": st_.utr,
                "a": st_.amount.amount,
                "t": st_.tds.amount,
                "m": st_.mode.value,
                "p": st_.paid_on,
                "r": json.dumps(st_.model_dump(mode="json")),
            },
        )
    ).first()
    if ins is not None:
        await s.execute(
            text("UPDATE claim_case SET settled_amount=:a, settled_at=:t WHERE id=:i"),
            {
                "a": st_.amount.amount,
                "t": datetime.combine(st_.paid_on, datetime.min.time(), UTC),
                "i": case.id,
            },
        )
        await apply_if_valid(uow, case, "settled", hub, "settled")
        await audit.append(
            s,
            case.id,
            "callback.settlement",
            {
                "amount": str(st_.amount.amount),
                "utr_sha256": __import__("hashlib").sha256(st_.utr.encode()).hexdigest(),
            },
            actor_type="external",
            actor_id="insurer",
        )

    async def pub() -> None:
        await hub.publish("settlement.received", str(case.id), {"amount": str(st_.amount.amount)})

    uow.after_commit(pub)
    return 204, None
