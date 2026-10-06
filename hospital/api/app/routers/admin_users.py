from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.auth.deps import require_role
from app.auth.principal import Principal
from app.auth.users import invalidate
from app.core.deps import get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit

router = APIRouter(prefix="/v1/admin", tags=["admin"])
Admin = Depends(require_role("admin"))


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


def _user(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "email": r.email,
        "display_name": r.display_name,
        "role": r.role,
        "active": r.active,
        "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
    }


@router.get("/users", operation_id="listUsers")
async def list_users(
    role: str | None = None,
    active: bool | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    _: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    where = (
        "(CAST(:role AS text) IS NULL OR role = :role) AND (CAST(:active AS boolean) IS NULL OR active = :active) AND "
        "(CAST(:q AS text) IS NULL OR email ILIKE :q2 OR display_name ILIKE :q2)"
    )
    params = {"role": role, "active": active, "q": q, "q2": f"%{q}%" if q else None}
    count_sql = text("SELECT count(*) FROM app_user WHERE " + where)  # noqa: S608 (static fragment)
    page_sql = text("SELECT * FROM app_user WHERE " + where + " ORDER BY email LIMIT :l OFFSET :o")  # noqa: S608
    total = (await uow.session.execute(count_sql, params)).scalar()
    rows = (
        await uow.session.execute(page_sql, {**params, "l": size, "o": (page - 1) * size})
    ).all()
    return {"items": [_user(r) for r in rows], "page": page, "size": size, "total": total}


@router.patch("/users/{user_id}", operation_id="patchUser")
async def patch_user(
    user_id: str,
    body: UserPatch,
    request: Request,
    p: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    row = (
        (
            await uow.session.execute(
                text("SELECT * FROM app_user WHERE id = CAST(:i AS uuid) FOR UPDATE"),
                {"i": user_id},
            )
        ).one_or_none()
        if _is_uuid(user_id)
        else None
    )
    if row is None:
        raise ApiError("not_found", "unknown user")
    if not body.active:
        if str(row.id) == str(p.id):
            raise ApiError("cannot_deactivate_self", "admins cannot deactivate themselves")
        if row.role == "admin":
            others = (
                await uow.session.execute(
                    text(
                        "SELECT count(*) FROM app_user WHERE role='admin' AND active AND id <> :i"
                    ),
                    {"i": row.id},
                )
            ).scalar()
            if not others:
                raise ApiError("last_admin", "the last active admin cannot be deactivated")
    await uow.session.execute(
        text("UPDATE app_user SET active = :a WHERE id = :i"), {"a": body.active, "i": row.id}
    )
    await audit.append(
        uow.session,
        audit.SYSTEM_CASE,
        "user.deactivated",
        {"user_id": str(row.id), "active": body.active},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    await invalidate(request.app.state.redis, row.keycloak_sub)  # takes effect immediately
    updated = (
        await uow.session.execute(text("SELECT * FROM app_user WHERE id = :i"), {"i": row.id})
    ).one()
    return _user(updated)


@router.get("/users/{user_id}/activity", operation_id="userActivity")
async def user_activity(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    _: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    if not _is_uuid(user_id):
        raise ApiError("not_found", "unknown user")
    rows = (
        await uow.session.execute(
            text(
                "SELECT ts, event_type, case_id FROM audit_event WHERE actor_id = :a ORDER BY ts DESC LIMIT :l"
            ),
            {"a": user_id, "l": limit},
        )
    ).all()
    return {
        "events": [
            {"ts": r.ts.isoformat(), "type": r.event_type, "case_id": str(r.case_id)} for r in rows
        ]
    }


def _is_uuid(v: str) -> bool:
    import uuid

    try:
        uuid.UUID(v)
        return True
    except ValueError:
        return False
