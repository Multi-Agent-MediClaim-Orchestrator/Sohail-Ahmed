"""ConfigService (01-03 §6-7): resolve as-of a timestamp with scope fallback, in-process TTL cache with pub/sub
invalidation, and the draft -> validate -> dry-run -> publish -> retire lifecycle with the two-person rule."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from claim_contract import audit
from claim_contract.errors import ProblemError
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ids import uuid7
from .schemas import DOMAIN_SCHEMA, SCHEMAS, TWO_PERSON_DOMAINS

CACHE_TTL = 60.0
CHANNEL = "config.changed"


class ConfigNotFound(Exception):
    def __init__(self, domain: str, name: str) -> None:
        super().__init__(f"no published config for {domain}/{name}")
        self.domain, self.name = domain, name


@dataclass(frozen=True)
class ResolvedConfig:
    domain: str
    name: str
    version: int
    payload: Any  # validated pydantic model
    checksum: str
    version_id: UUID | None = None


def checksum_of(payload: dict[str, Any]) -> str:
    return hashlib.sha256(audit.canonical_json(payload).encode()).hexdigest()


class ConfigService:
    def __init__(self, redis: Any | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._cache: dict[tuple[str, str, str], tuple[float, ResolvedConfig]] = {}
        self.redis, self.clock = redis, clock

    # ------------------------------------------------------------------ resolve
    def _key(self, domain: str, name: str, at: datetime | None) -> tuple[str, str, str]:
        if at is None:
            return domain, name, self.clock().replace(second=0, microsecond=0).isoformat()
        return domain, name, at.isoformat()

    def invalidate(self, domain: str | None = None, name: str | None = None) -> None:
        for k in list(self._cache):
            if (domain is None or k[0] == domain) and (name is None or k[1] == name):
                del self._cache[k]

    async def resolve(self, session: AsyncSession, domain: str, name: str = "default", at: datetime | None = None) -> ResolvedConfig:
        key = self._key(domain, name, at)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        when = at or self.clock()
        row = (
            await session.execute(
                text(
                    "SELECT cv.id, cv.version, cv.payload, cv.payload_schema, cv.checksum FROM config.config_version cv "
                    "JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain = :d AND cs.name = :n "
                    "AND cv.status IN ('published','retired') AND cv.effective_from <= :t "
                    "AND (cv.effective_to IS NULL OR cv.effective_to > :t) ORDER BY cv.effective_from DESC LIMIT 1"
                ),
                {"d": domain, "n": name, "t": when},
            )
        ).one_or_none()
        if row is None:
            if name != "default":
                return await self.resolve(session, domain, "default", at)
            raise ConfigNotFound(domain, name)
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        model = SCHEMAS[row.payload_schema].model_validate(payload)  # fail closed if the schema evolved
        cfg = ResolvedConfig(domain, name, row.version, model, row.checksum, row.id)
        self._cache[key] = (time.monotonic(), cfg)
        return cfg

    async def resolve_scoped(self, session: AsyncSession, domain: str, scopes: list[str], at: datetime | None = None) -> ResolvedConfig:
        """Try each scope in order (e.g. ``cashless/planned/cardiac`` -> ``cashless/planned/default`` -> ``default``)."""
        last: ConfigNotFound | None = None
        for s in scopes:
            try:
                row = await session.execute(
                    text("SELECT 1 FROM config.config_set WHERE domain = :d AND name = :n"), {"d": domain, "n": s}
                )
                if row.first() is None:
                    continue
                return await self.resolve(session, domain, s, at)
            except ConfigNotFound as exc:
                last = exc
        raise last or ConfigNotFound(domain, scopes[-1] if scopes else "default")

    async def subscribe(self) -> None:  # pragma: no cover - needs a live redis
        if self.redis is None:
            return
        ps = self.redis.pubsub()
        await ps.subscribe(CHANNEL)
        async for msg in ps.listen():
            if msg["type"] == "message":
                d = json.loads(msg["data"])
                self.invalidate(d.get("domain"), d.get("name"))

    async def _announce(self, domain: str, name: str, version: int) -> None:
        self.invalidate(domain, name)
        if self.redis is not None:
            try:
                await self.redis.publish(CHANNEL, json.dumps({"domain": domain, "name": name, "version": version}))
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ lifecycle
    async def ensure_set(self, session: AsyncSession, domain: str, name: str, created_by: str) -> UUID:
        row = (await session.execute(text("SELECT id FROM config.config_set WHERE domain=:d AND name=:n"), {"d": domain, "n": name})).one_or_none()
        if row:
            return row.id
        sid = uuid7()
        await session.execute(
            text("INSERT INTO config.config_set (id, domain, name, created_by) VALUES (:i,:d,:n,:c)"),
            {"i": sid, "d": domain, "n": name, "c": created_by},
        )
        return sid

    def validate_payload(self, domain: str, payload: dict[str, Any]) -> BaseModel:
        schema_id = DOMAIN_SCHEMA.get(domain)
        if schema_id is None:
            raise ProblemError("validation_error", f"unknown config domain {domain!r}")
        try:
            body = {"schema_id": schema_id, **{k: v for k, v in payload.items() if k != "schema_id"}}
            return SCHEMAS[schema_id].model_validate(body)
        except ValidationError as exc:
            raise ProblemError(
                "validation_error", "config payload failed validation",
                errors=[{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()],
            ) from exc

    async def create_draft(self, session: AsyncSession, domain: str, name: str, payload: dict[str, Any], by: str, note: str) -> dict[str, Any]:
        model = self.validate_payload(domain, payload)
        canonical = json.loads(model.model_dump_json(by_alias=True))
        sid = await self.ensure_set(session, domain, name, by)
        v = (await session.execute(text("SELECT coalesce(max(version),0)+1 AS v FROM config.config_version WHERE config_set_id=:s"), {"s": sid})).scalar_one()
        vid = uuid7()
        await session.execute(
            text(
                "INSERT INTO config.config_version (id, config_set_id, version, status, payload, payload_schema, checksum, change_note, created_by) "
                "VALUES (:i,:s,:v,'draft',CAST(:p AS JSONB),:ps,:c,:n,:by)"
            ),
            {"i": vid, "s": sid, "v": v, "p": json.dumps(canonical), "ps": DOMAIN_SCHEMA[domain], "c": checksum_of(canonical), "n": note, "by": by},
        )
        return {"id": vid, "version": v, "checksum": checksum_of(canonical)}

    async def publish(
        self, session: AsyncSession, domain: str, name: str, version: int, by: str, *, second_approver: str | None = None,
        effective_from: datetime | None = None, if_match: str | None = None, case_id_for_audit: UUID | None = None,
    ) -> None:
        row = (
            await session.execute(
                text(
                    "SELECT cv.id, cv.status, cv.checksum, cv.created_by, cv.config_set_id FROM config.config_version cv "
                    "JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v FOR UPDATE OF cv"
                ),
                {"d": domain, "n": name, "v": version},
            )
        ).one_or_none()
        if row is None:
            raise ProblemError("not_found", "unknown config version")
        if row.status != "draft":
            raise ProblemError("invalid_transition", f"version is {row.status}, not draft")
        if if_match is not None and if_match != row.checksum:
            raise ProblemError("stale_etag", "draft changed since you loaded it", status=412)
        if domain in TWO_PERSON_DOMAINS and (not second_approver or second_approver == by):
            raise ProblemError("validation_error", "second_approver_required: a different person must co-approve", status=422)
        eff = effective_from or self.clock()
        # close the previously published window in the same transaction
        await session.execute(
            text(
                "UPDATE config.config_version SET effective_to = :e WHERE config_set_id = :s AND status = 'published' "
                "AND (effective_to IS NULL OR effective_to > :e) AND effective_from < :e"
            ),
            {"e": eff, "s": row.config_set_id},
        )
        try:
            await session.execute(
                text(
                    "UPDATE config.config_version SET status='published', effective_from=:e, published_by=:b, second_approver=:sa, "
                    "published_at=now() WHERE id=:i"
                ),
                {"e": eff, "b": by, "sa": second_approver, "i": row.id},
            )
        except Exception as exc:
            if "no_overlap" in str(exc):
                raise ProblemError("idempotency_conflict", "overlapping published window", status=409) from exc
            raise
        await self._announce(domain, name, version)

    async def retire(self, session: AsyncSession, domain: str, name: str, version: int, at: datetime | None = None) -> None:
        await session.execute(
            text(
                "UPDATE config.config_version cv SET status='retired', effective_to = coalesce(cv.effective_to, :t) "
                "FROM config.config_set cs WHERE cs.id = cv.config_set_id AND cs.domain=:d AND cs.name=:n AND cv.version=:v AND cv.status='published'"
            ),
            {"t": at or self.clock(), "d": domain, "n": name, "v": version},
        )
        await self._announce(domain, name, version)

    async def versions(self, session: AsyncSession, domain: str, name: str) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                text(
                    "SELECT cv.version, cv.status, cv.checksum, cv.effective_from, cv.effective_to, cv.created_by, cv.published_by, cv.second_approver, cv.change_note "
                    "FROM config.config_version cv JOIN config.config_set cs ON cs.id=cv.config_set_id WHERE cs.domain=:d AND cs.name=:n ORDER BY cv.version"
                ),
                {"d": domain, "n": name},
            )
        ).all()
        return [dict(r._mapping) for r in rows]


def stamp_versions(**resolved: ResolvedConfig) -> dict[str, int]:
    """``InsurerConfigVersions`` stamp stored on runs/decisions (01-03 §5)."""
    return {k: v.version for k, v in resolved.items()}


Dryrunner = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
