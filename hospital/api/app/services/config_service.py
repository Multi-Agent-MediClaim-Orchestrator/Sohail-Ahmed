"""Versioned config resolution (01-03): every decision records the versions it used."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SNAPSHOT_DOMAINS = ("doc_requirements", "deadlines", "router_rules", "confidence_gates")
RESOLVE = text("""
SELECT cv.version, cv.payload FROM config_version cv JOIN config_set cs ON cs.id = cv.config_set_id
WHERE cs.domain = :d AND cs.name = :n AND cv.status IN ('published', 'retired')
  AND cv.effective_from <= :at AND (cv.effective_to IS NULL OR cv.effective_to > :at)
ORDER BY cv.effective_from DESC LIMIT 1""")


class ConfigNotFound(LookupError):
    pass


async def resolve(
    session: AsyncSession, domain: str, name: str = "default", at: datetime | None = None
) -> tuple[int, dict[str, Any]]:
    at = at or datetime.now(UTC)
    row = (await session.execute(RESOLVE, {"d": domain, "n": name, "at": at})).first()
    if row is None and name != "default":
        return await resolve(session, domain, "default", at)
    if row is None:
        raise ConfigNotFound(f"{domain}/{name}")
    return row.version, row.payload


async def snapshot(
    session: AsyncSession, domains: tuple[str, ...] = SNAPSHOT_DOMAINS
) -> dict[str, int]:
    return {d: (await resolve(session, d))[0] for d in domains}


LOAD_VERSION = text("""
SELECT cv.version, cv.payload FROM config_version cv JOIN config_set cs ON cs.id = cv.config_set_id
WHERE cs.domain = :d AND cs.name = :n AND cv.version = :v AND cv.status IN ('published', 'retired')""")


async def load_version(
    session: AsyncSession, domain: str, version: int, name: str = "default"
) -> dict[str, Any]:
    row = (await session.execute(LOAD_VERSION, {"d": domain, "n": name, "v": version})).first()
    if row is None:
        raise ConfigNotFound(f"{domain}/{name}@{version}")
    return row.payload  # type: ignore[no-any-return]
