"""Case status changes: contract state machine + manual whitelist + history + audit + SSE."""

from __future__ import annotations

import uuid
from typing import Any

from claim_contract.enums import HospitalCaseStatus as S
from claim_contract.transitions import HOSPITAL_TRANSITIONS, InvalidTransition, assert_hospital
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth.principal import Principal
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit

MANUAL_WHITELIST: dict[tuple[str, str], set[str]] = {
    ("docs_pending", "officer"): {"docs_complete", "closed"},
    ("docs_complete", "officer"): {"docs_pending", "closed"},
    ("docs_complete", "desk"): {"docs_pending"},
    ("draft", "officer"): {"closed"},
}


def allowed_for(status: str, roles: frozenset[str] | set[str]) -> list[str]:
    contract = {t.value for t in HOSPITAL_TRANSITIONS[S(status)]}
    manual: set[str] = set()
    for r in roles:
        manual |= MANUAL_WHITELIST.get((status, r), set())
    return sorted(contract & manual)


async def transition(
    uow: UoW,
    case_id: uuid.UUID | str,
    to: str,
    actor: Principal | None,
    *,
    reason: str | None = None,
    manual: bool = False,
    hub: Any = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Move a case to `to` inside the caller's transaction. `manual` applies the role whitelist."""
    row = (
        await uow.session.execute(
            text(
                "SELECT id, status::text AS status, version FROM claim_case WHERE id = :i FOR NO KEY UPDATE"
            ),
            {"i": uuid.UUID(str(case_id))},
        )
    ).one_or_none()
    if row is None:
        raise ApiError("not_found", "unknown case")
    if expected_version is not None and row.version != expected_version:
        raise ApiError("precondition_failed", "case was modified; reload and retry")
    try:
        assert_hospital(S(row.status), S(to))
    except InvalidTransition:
        raise ApiError(
            "invalid_transition",
            f"{row.status} -> {to} is not allowed",
            allowed=sorted(t.value for t in HOSPITAL_TRANSITIONS[S(row.status)]),
        ) from None
    if manual:
        roles = actor.roles if actor else frozenset()
        if to not in allowed_for(row.status, roles):
            raise ApiError(
                "invalid_transition",
                f"{row.status} -> {to} is not a permitted manual transition",
                allowed=allowed_for(row.status, roles),
            )
    actor_id = actor.actor_id if actor else "system"
    try:
        await uow.session.execute(
            text(
                "UPDATE claim_case SET status = CAST(:to AS hospital_case_status), version = version + 1, "
                "closed_at = CASE WHEN :to = 'closed' THEN now() ELSE closed_at END WHERE id = :i"
            ),
            {"to": to, "i": row.id},
        )
    except DBAPIError as e:  # DB trigger is the safety net
        if "invalid_transition" in str(e):
            raise ApiError(
                "invalid_transition", f"{row.status} -> {to} rejected by database"
            ) from None
        raise
    await uow.session.execute(
        text(
            "INSERT INTO case_status_history (id, case_id, from_status, to_status, actor_id, reason) "
            "VALUES (uuid_generate_v7(), :c, CAST(:f AS hospital_case_status), CAST(:t AS hospital_case_status), "
            ":a, :r)"
        ),
        {"c": row.id, "f": row.status, "t": to, "a": actor_id, "r": reason},
    )
    await audit.append(
        uow.session,
        row.id,
        "case.status_changed",
        {"from": row.status, "to": to, "reason": reason},
        actor_type=actor.actor_type if actor else "system",
        actor_id=actor_id,
    )
    if hub is not None:

        async def publish() -> None:
            await hub.publish("case.status_changed", str(row.id), {"from": row.status, "to": to})

        uow.after_commit(publish)
    return {"from": row.status, "to": to, "version": row.version + 1}
