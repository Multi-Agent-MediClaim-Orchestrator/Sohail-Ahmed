"""Admin endpoints: config lifecycle (01-03 §7), ops health, metrics, client error sink (03-10 §11)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from claim_contract.errors import ProblemError
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .. import metrics
from ..config.schemas import DOMAIN_SCHEMA, SCHEMAS
from ..config.service import ConfigNotFound
from ..db import sessionmaker, transaction
from ..security.auth import Principal, require_roles
from ..services import audit, dryrun, orchestrator
from ..services.events import publish
from ..settings import get_settings

router = APIRouter(tags=["admin"])
admin = require_roles("admin")
staff = require_roles("reviewer", "senior_reviewer", "approver", "admin")
log = logging.getLogger("client-errors")
CONFIG_CHAIN = UUID("00000000-0000-7000-8000-00000000c0f6")  # global audit chain for admin/config events (no case)


@router.get("/v1/admin/config/domains")
async def domains(p: Principal = Depends(admin)) -> dict[str, Any]:
    return {"domains": sorted(DOMAIN_SCHEMA)}


@router.get("/v1/admin/config/{domain}/schema")
async def schema(domain: str, p: Principal = Depends(admin)) -> dict[str, Any]:
    if domain not in DOMAIN_SCHEMA:
        raise ProblemError("not_found", "unknown domain", status=404)
    return SCHEMAS[DOMAIN_SCHEMA[domain]].model_json_schema()


@router.get("/v1/admin/config/{domain}")
async def list_sets(domain: str, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        rows = (await s.execute(text("SELECT name FROM config.config_set WHERE domain = :d ORDER BY name"), {"d": domain})).all()
    return {"domain": domain, "names": [r.name for r in rows]}


@router.get("/v1/admin/config/{domain}/{name}/versions")
async def versions(domain: str, name: str, request: Request, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        v = await request.app.state.config.versions(s, domain, name)
    return {"items": [{**x, "effective_from": x["effective_from"].isoformat() if x["effective_from"] else None,
                       "effective_to": x["effective_to"].isoformat() if x["effective_to"] else None} for x in v]}


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]
    change_note: str


@router.post("/v1/admin/config/{domain}/{name}/versions", status_code=201)
async def create_draft(domain: str, name: str, body: DraftBody, request: Request, p: Principal = Depends(admin)) -> dict[str, Any]:
    if len(body.change_note.strip()) < 5:
        raise ProblemError("validation_error", "change_note is required", status=422)
    async with transaction() as tx:
        out = await request.app.state.config.create_draft(tx.session, domain, name, body.payload, p.sub, body.change_note)
        await audit.append(tx.session, CONFIG_CHAIN, "config.drafted", actor_type="human", actor_id=p.sub, payload={"domain": domain, "name": name, "version": out["version"], "checksum": out["checksum"]})
    return {"id": str(out["id"]), "version": out["version"], "checksum": out["checksum"]}


@router.put("/v1/admin/config/{domain}/{name}/versions/{version}")
async def update_draft(domain: str, name: str, version: int, body: DraftBody, request: Request, if_match: str | None = Header(default=None), p: Principal = Depends(admin)) -> dict[str, Any]:
    cfg = request.app.state.config
    model = cfg.validate_payload(domain, body.payload)
    from ..config.service import checksum_of

    canonical = json.loads(model.model_dump_json(by_alias=True))
    async with transaction() as tx:
        row = (await tx.session.execute(text(
            "SELECT cv.id, cv.status, cv.checksum FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v FOR UPDATE OF cv"),
            {"d": domain, "n": name, "v": version})).one_or_none()
        if row is None:
            raise ProblemError("not_found", "unknown version", status=404)
        if row.status != "draft":
            raise ProblemError("invalid_transition", "only drafts can be edited", status=409)
        if if_match is not None and if_match.strip('"') != row.checksum:
            raise ProblemError("stale_etag", "draft changed since you loaded it", status=412)
        await tx.session.execute(text("UPDATE config.config_version SET payload = CAST(:p AS JSONB), checksum = :c, change_note = :n WHERE id = :i"),
                                 {"p": json.dumps(canonical), "c": checksum_of(canonical), "n": body.change_note, "i": row.id})
    return {"checksum": checksum_of(canonical)}


@router.post("/v1/admin/config/{domain}/{name}/versions/{version}:validate")
async def validate(domain: str, name: str, version: int, request: Request, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        row = (await s.execute(text("SELECT cv.payload FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v"),
                               {"d": domain, "n": name, "v": version})).one_or_none()
    if row is None:
        raise ProblemError("not_found", "unknown version", status=404)
    request.app.state.config.validate_payload(domain, row.payload if isinstance(row.payload, dict) else json.loads(row.payload))
    return {"valid": True}


@router.post("/v1/admin/config/{domain}/{name}/versions/{version}:dry-run")
async def dry_run(domain: str, name: str, version: int, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with transaction() as tx:
        return await dryrun.run(tx.session, domain, name, version)


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second_approver: str | None = None
    effective_from: datetime | None = None


@router.post("/v1/admin/config/{domain}/{name}/versions/{version}:publish")
async def publish_version(domain: str, name: str, version: int, body: PublishBody, request: Request, if_match: str | None = Header(default=None), p: Principal = Depends(admin)) -> dict[str, Any]:
    cfg = request.app.state.config
    async with transaction() as tx:
        await cfg.publish(tx.session, domain, name, version, p.sub, second_approver=body.second_approver, effective_from=body.effective_from, if_match=if_match.strip('"') if if_match else None)
        ck = (await tx.session.execute(text("SELECT cv.checksum FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v"),
                                       {"d": domain, "n": name, "v": version})).scalar_one()
        await audit.append(tx.session, CONFIG_CHAIN, "config.published", actor_type="human", actor_id=p.sub,
                           payload={"domain": domain, "name": name, "version": version, "checksum": ck, "second_approver": body.second_approver})
    await publish("config.published", None, {"domain": domain, "name": name, "version": version}, ["admin"])
    return {"published": True, "version": version}


@router.post("/v1/admin/config/{domain}/{name}/versions/{version}:retire")
async def retire(domain: str, name: str, version: int, request: Request, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with transaction() as tx:
        await request.app.state.config.retire(tx.session, domain, name, version)
        await audit.append(tx.session, CONFIG_CHAIN, "config.retired", actor_type="human", actor_id=p.sub, payload={"domain": domain, "name": name, "version": version})
    return {"retired": True}


@router.get("/v1/admin/config/{domain}/{name}/versions/{version}/diff")
async def diff(domain: str, name: str, version: int, against: int, p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        rows = (await s.execute(text(
            "SELECT cv.version, cv.payload FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version IN (:a,:b)"),
            {"d": domain, "n": name, "a": version, "b": against})).all()
    by = {r.version: (r.payload if isinstance(r.payload, dict) else json.loads(r.payload)) for r in rows}
    if version not in by or against not in by:
        raise ProblemError("not_found", "unknown version", status=404)

    def walk(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
        if isinstance(a, dict) and isinstance(b, dict):
            out: list[dict[str, Any]] = []
            for k in sorted(set(a) | set(b)):
                out += walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
            return out
        return [] if a == b else [{"path": path, "from": b, "to": a}]

    return {"changes": walk(by[version], by[against])}


# ------------------------------------------------------------------ ops
@router.get("/v1/ops/health")
async def ops_health(request: Request, p: Principal = Depends(staff)) -> dict[str, Any]:
    s_ = get_settings()
    out: dict[str, Any] = {}
    try:
        async with sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
        out["db"] = "ok"
    except Exception:
        out["db"] = "down"
    r = getattr(request.app.state, "redis", None)
    try:
        out["redis"] = "ok" if r is not None and await r.ping() else "down"
    except Exception:
        out["redis"] = "down"
    out["calc_engine"] = "in-process" if not s_.calc_engine_url.startswith("http") else "remote"
    out["crew"] = "configured" if s_.crew_url else "not configured"
    out["orchestrator"] = s_.orchestrator
    async with sessionmaker()() as s:
        out["outbox"] = {k: v for k, v in (await s.execute(text("SELECT status, count(*) FROM ops.outbox GROUP BY 1"))).all()}
    out["time"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


@router.get("/v1/ops/dead-letters")
async def dead_letters(p: Principal = Depends(admin)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        rows = (await s.execute(text("SELECT id, case_id, endpoint, attempts, last_error, created_at FROM ops.outbox WHERE status = 'dead' ORDER BY created_at"))).all()
    return {"items": [{"id": str(r.id), "case_id": str(r.case_id), "endpoint": r.endpoint, "attempts": r.attempts, "last_error": r.last_error, "created_at": r.created_at.isoformat()} for r in rows]}


class DeadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str  # retry | discard
    comment: str = ""


@router.post("/v1/ops/dead-letters/{oid}")
async def dead_action(oid: str, body: DeadAction, p: Principal = Depends(admin)) -> dict[str, Any]:
    if body.action == "discard" and len(body.comment.strip()) < 5:
        raise ProblemError("validation_error", "discarding needs a comment", status=422)
    async with transaction() as tx:
        if body.action == "retry":  # same idempotency key, attempts reset
            await tx.session.execute(text("UPDATE ops.outbox SET status = 'pending', attempts = 0, next_attempt_at = now() WHERE id = CAST(:i AS UUID) AND status = 'dead'"), {"i": oid})
        elif body.action == "discard":
            await tx.session.execute(text("UPDATE ops.outbox SET status = 'sent', last_error = :c WHERE id = CAST(:i AS UUID) AND status = 'dead'"), {"i": oid, "c": f"discarded by {p.sub}: {body.comment}"})
        else:
            raise ProblemError("validation_error", "action must be retry or discard", status=422)
    return {"ok": True}


@router.get("/metrics")
async def prom() -> Response:
    from ..jobs_cron import refresh_outbox_gauges

    try:
        await refresh_outbox_gauges()
    except Exception:
        pass
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


class ClientError(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: str = ""
    route: str = ""
    kind: str = "error"


@router.post("/v1/client-errors", status_code=204)
async def client_errors(body: ClientError, p: Principal = Depends(staff)) -> Response:
    log.warning("ui error kind=%s route=%s msg=%s", body.kind, body.route[:80], body.message[:200])  # no PII is expected; route has ids only
    return Response(status_code=204)


_ = (audit, orchestrator, ConfigNotFound)
