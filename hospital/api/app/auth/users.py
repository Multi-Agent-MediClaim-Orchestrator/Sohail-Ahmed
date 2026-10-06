"""Just-in-time user provisioning with a short Redis cache (doc 02 §6.1)."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import uuid_utils
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError

UPSERT = text("""
INSERT INTO app_user (id, keycloak_sub, email, display_name, role, last_login_at)
VALUES (:id, :sub, :email, :name, :role, now())
ON CONFLICT (keycloak_sub) DO UPDATE
  SET email = EXCLUDED.email, display_name = EXCLUDED.display_name, role = EXCLUDED.role,
      last_login_at = CASE WHEN app_user.last_login_at IS NULL
                           OR app_user.last_login_at < now() - interval '5 minutes'
                           THEN now() ELSE app_user.last_login_at END
RETURNING id, active
""")


def cache_key(sub: str) -> str:
    return f"cache:hosp:user:{sub}"


async def resolve_user(
    session: AsyncSession, redis: Any, *, sub: str, email: str, name: str, role: str, ttl: int
) -> tuple[uuid.UUID, bool, bool]:
    """Return (user_id, active, first_seen_in_cache). Skips the DB while the profile cache is fresh."""
    h = hashlib.sha256(f"{email}|{name}|{role}".encode()).hexdigest()[:16]
    raw = await redis.get(cache_key(sub))
    if raw is not None:
        d = json.loads(raw)
        if d["h"] == h:
            return uuid.UUID(d["id"]), d["active"], False
    clash = (
        await session.execute(
            text("SELECT 1 FROM app_user WHERE email = :e AND keycloak_sub <> :s LIMIT 1"),
            {"e": email, "s": sub},
        )
    ).first()
    if clash:
        raise ApiError("email_conflict", "another account already uses this email")
    row = (
        await session.execute(
            UPSERT,
            {
                "id": uuid.UUID(str(uuid_utils.uuid7())),
                "sub": sub,
                "email": email,
                "name": name,
                "role": role,
            },
        )
    ).one()
    await session.commit()
    await redis.set(
        cache_key(sub), json.dumps({"id": str(row.id), "h": h, "active": row.active}), ex=ttl
    )
    return row.id, row.active, True


async def invalidate(redis: Any, sub: str) -> None:
    await redis.delete(cache_key(sub))
