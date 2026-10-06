"""Config lifecycle (01-03 §5): draft -> validate -> dry-run -> two-person publish -> retire."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from claim_contract.audit import canonical_json
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from app.auth.principal import Principal
from app.completeness import service as comp
from app.completeness.rules import evaluate
from app.completeness.schemas import DocRequirementsConfig, validate_payload
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Deadlines(Strict):
    reminder_offsets_hours: list[int] = Field(min_length=1, max_length=10)
    reimbursement_filing_days: int = Field(gt=0, le=365)
    query_response_sla_hours: int = Field(gt=0, le=720)
    request_sla_hours: int = Field(gt=0, le=720)
    intimation_emergency_hours: int = Field(gt=0, le=168)
    planned_preauth_lead_hours: int = Field(default=48, gt=0, le=720)


class Gates(Strict):
    parse_min: float = Field(ge=0, le=1)
    classification_min: float = Field(ge=0, le=1)
    agreement_min: float = Field(ge=0, le=1)
    quality_min: float = Field(ge=0, le=1)
    amount_tolerance_inr: float = Field(ge=0)


class Signoff(Strict):
    four_eyes: bool
    ack_required: bool


class QueryPolicy(Strict):
    max_rounds: int = Field(ge=1, le=3)
    default_sla_hours: int = Field(gt=0, le=720)
    hospital_round3_two_person: bool


class RouterRules(Strict):
    claim_type_rules: list[dict[str, Any]] = Field(min_length=1)
    emergency_rules: dict[str, Any]
    flag_rules: list[dict[str, Any]]
    step_templates: dict[str, list[str]]


MODELS: dict[str, type[BaseModel]] = {
    "deadlines": Deadlines,
    "confidence_gates": Gates,
    "signoff": Signoff,
    "query_policy": QueryPolicy,
    "router_rules": RouterRules,
}
DOMAINS = ("doc_requirements", *MODELS)


def schema_for(domain: str) -> dict[str, Any]:
    if domain == "doc_requirements":
        return DocRequirementsConfig.model_json_schema()
    return MODELS[domain].model_json_schema()


def validate(domain: str, payload: dict[str, Any]) -> list[str]:
    if domain not in DOMAINS:
        raise ApiError("not_found", f"unknown config domain {domain}")
    if domain == "doc_requirements":
        return validate_payload(payload)[1]
    if domain == "router_rules":
        from app.router_engine.schema import validate_rules

        return validate_rules(payload)
    try:
        MODELS[domain].model_validate(payload)
    except ValidationError as e:
        return [f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors()]
    return []


def checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _view(r: Any) -> dict[str, Any]:
    return {
        "domain": r.domain,
        "name": r.name,
        "version": r.version,
        "status": r.status,
        "effective_from": r.effective_from.isoformat() if r.effective_from else None,
        "effective_to": r.effective_to.isoformat() if r.effective_to else None,
        "change_note": r.change_note,
        "created_by": r.created_by,
        "published_by": r.published_by,
        "second_approver": r.second_approver,
        "checksum": r.checksum,
    }


SELECT = (
    "SELECT cs.domain, cs.name, cv.version, cv.status::text AS status, cv.effective_from, cv.effective_to, "
    "cv.change_note, cv.created_by, cv.published_by, cv.second_approver, cv.checksum, cv.payload, cv.id "
    "FROM config_version cv JOIN config_set cs ON cs.id = cv.config_set_id "
)


async def list_versions(uow: UoW, domain: str, name: str = "default") -> dict[str, Any]:
    if domain not in DOMAINS:
        raise ApiError("not_found", f"unknown config domain {domain}")
    rows = (
        await uow.session.execute(
            text(SELECT + "WHERE cs.domain=:d AND cs.name=:n ORDER BY cv.version DESC"),
            {"d": domain, "n": name},
        )
    ).all()
    return {"domain": domain, "name": name, "versions": [_view(r) for r in rows]}


async def get_version(
    uow: UoW, domain: str, version: int, name: str = "default", *, lock: bool = False
) -> Any:
    row = (
        await uow.session.execute(
            text(
                SELECT
                + "WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v"
                + (" FOR UPDATE OF cv" if lock else "")
            ),
            {"d": domain, "n": name, "v": version},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", f"unknown version {domain}@{version}")
    return row


async def create_draft(
    uow: UoW, p: Principal, domain: str, payload: dict[str, Any], note: str, name: str = "default"
) -> dict[str, Any]:
    errs = validate(domain, payload)
    hard = [e for e in errs]
    # schema-level failures cannot even be stored as a draft; semantic ones can (validate reports them)
    if domain == "doc_requirements" and validate_payload(payload)[0] is None:
        raise ApiError(
            "config_invalid",
            "payload does not match the schema",
            errors=[{"message": e} for e in hard],
        )
    if domain != "doc_requirements" and hard:
        raise ApiError(
            "config_invalid",
            "payload does not match the schema",
            errors=[{"message": e} for e in hard],
        )
    s = uow.session
    await s.execute(
        text(
            "INSERT INTO config_set (id, domain, name, description, created_by) VALUES "
            "(uuid_generate_v7(), :d, :n, :d, :u) ON CONFLICT (domain, name) DO NOTHING"
        ),
        {"d": domain, "n": name, "u": str(p.id)},
    )
    set_id = (
        await s.execute(
            text("SELECT id FROM config_set WHERE domain=:d AND name=:n"), {"d": domain, "n": name}
        )
    ).scalar()
    await s.execute(
        text("SELECT id FROM config_set WHERE id=:i FOR UPDATE"), {"i": set_id}
    )  # serialise version numbering
    ver = (
        await s.execute(
            text("SELECT COALESCE(max(version), 0) + 1 FROM config_version WHERE config_set_id=:i"),
            {"i": set_id},
        )
    ).scalar()
    await s.execute(
        text(
            "INSERT INTO config_version (id, config_set_id, version, status, payload, payload_schema, checksum, change_note, "
            "created_by) VALUES (uuid_generate_v7(), :s, :v, 'draft', CAST(:p AS jsonb), :ps, :ck, :n, :u)"
        ),
        {
            "s": set_id,
            "v": ver,
            "p": json.dumps(payload),
            "ps": f"{domain}@1",
            "ck": checksum(payload),
            "n": note,
            "u": str(p.id),
        },
    )
    await audit.append(
        s,
        audit.SYSTEM_CASE,
        "config.drafted",
        {"domain": domain, "name": name, "version": ver},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {
        "domain": domain,
        "name": name,
        "version": ver,
        "status": "draft",
        "semantic_errors": [e for e in errs] if domain == "doc_requirements" else [],
    }


async def validate_version(
    uow: UoW, p: Principal, domain: str, version: int, name: str = "default"
) -> dict[str, Any]:
    row = await get_version(uow, domain, version, name)
    errs = validate(domain, row.payload)
    await audit.append(
        uow.session,
        audit.SYSTEM_CASE,
        "config.validated",
        {"domain": domain, "version": version, "valid": not errs},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"valid": not errs, "errors": errs}


async def publish(
    uow: UoW,
    p: Principal,
    domain: str,
    version: int,
    redis: Any,
    name: str = "default",
    effective_from: datetime | None = None,
) -> dict[str, Any]:
    row = await get_version(uow, domain, version, name, lock=True)
    if row.status != "draft":
        raise ApiError("invalid_state", f"only drafts can be published (this one is {row.status})")
    errs = validate(domain, row.payload)
    if errs:
        raise ApiError(
            "config_invalid",
            "configuration failed validation",
            errors=[{"message": e} for e in errs],
        )
    if row.created_by == str(p.id):
        raise ApiError(
            "two_person_rule", "a different admin must publish a draft than the one who created it"
        )
    now = datetime.now(UTC)
    start = max(effective_from or now, now)
    s = uow.session
    set_id = (
        await s.execute(text("SELECT config_set_id FROM config_version WHERE id=:i"), {"i": row.id})
    ).scalar()
    await s.execute(
        text(
            "UPDATE config_version SET effective_to = :t WHERE config_set_id = :s AND status = 'published' "
            "AND effective_to IS NULL"
        ),
        {"t": start, "s": set_id},
    )
    await s.execute(
        text(
            "UPDATE config_version SET status='published', effective_from=:f, published_by=:p, second_approver=:c, "
            "published_at=now() WHERE id=:i"
        ),
        {"f": start, "p": str(p.id), "c": row.created_by, "i": row.id},
    )
    await audit.append(
        s,
        audit.SYSTEM_CASE,
        "config.published",
        {"domain": domain, "name": name, "version": version, "checksum": row.checksum},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    await redis.publish(
        "cfg:changed", json.dumps({"system": "hospital", "domain": domain, "version": version})
    )
    return {
        "domain": domain,
        "version": version,
        "status": "published",
        "effective_from": start.isoformat(),
    }


async def retire(
    uow: UoW, p: Principal, domain: str, version: int, name: str = "default"
) -> dict[str, Any]:
    row = await get_version(uow, domain, version, name, lock=True)
    if row.status != "published":
        raise ApiError("invalid_state", "only published versions can be retired")
    await uow.session.execute(
        text(
            "UPDATE config_version SET status='retired', effective_to=COALESCE(effective_to, now()) WHERE id=:i"
        ),
        {"i": row.id},
    )
    await audit.append(
        uow.session,
        audit.SYSTEM_CASE,
        "config.retired",
        {"domain": domain, "version": version},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"domain": domain, "version": version, "status": "retired"}


async def dry_run(
    uow: UoW, domain: str, version: int, sample: int, name: str = "default"
) -> dict[str, Any]:
    if domain != "doc_requirements":
        raise ApiError("validation_error", "dry-run is available for doc_requirements only")
    row = await get_version(uow, domain, version, name)
    cand, errs = validate_payload(row.payload)
    if cand is None or errs:
        raise ApiError(
            "config_invalid",
            "candidate configuration is invalid",
            errors=[{"message": e} for e in errs],
        )
    cur_ver, cur_payload = await comp.config_service.resolve(uow.session, "doc_requirements", name)
    current = DocRequirementsConfig.model_validate(cur_payload)
    ids = [
        r.id
        for r in (
            await uow.session.execute(
                text("SELECT id FROM claim_case ORDER BY created_at DESC LIMIT :n"), {"n": sample}
            )
        ).all()
    ]
    out: dict[str, Any] = {
        "evaluated": 0,
        "unchanged": 0,
        "changed": 0,
        "newly_blocked": [],
        "newly_cleared": [],
        "by_rule": {},
        "compared_to": cur_ver,
    }
    for cid in ids:
        case = (
            await uow.session.execute(
                text(
                    "SELECT c.*, c.status::text AS status_t, c.claim_type::text AS claim_type_t, "
                    "c.admission_type::text AS admission_type_t FROM claim_case c WHERE c.id=:i"
                ),
                {"i": cid},
            )
        ).first()
        now = datetime.now(UTC)
        before = evaluate(await comp.build_context(uow, case, current, now), current)
        after = evaluate(await comp.build_context(uow, case, cand, now), cand)
        out["evaluated"] += 1
        b = {i.rule_id: i for i in before.items}
        a = {i.rule_id: i for i in after.items}
        changed = False
        for rid in sorted(set(a) | set(b)):
            was = b[rid].severity == "blocker" if rid in b else False
            now_b = a[rid].severity == "blocker" if rid in a else False
            if was == now_b and (b.get(rid) and b[rid].status) == (a.get(rid) and a[rid].status):
                continue
            changed = True
            if now_b and not was:
                out["newly_blocked"].append(
                    {
                        "case_ref": case.claim_ref,
                        "rule_id": rid,
                        "reason": (a[rid].reasons or [""])[0],
                    }
                )
                out["by_rule"].setdefault(rid, {}).setdefault("newly_blocked", 0)
                out["by_rule"][rid]["newly_blocked"] += 1
            if was and not now_b:
                out["newly_cleared"].append({"case_ref": case.claim_ref, "rule_id": rid})
                out["by_rule"].setdefault(rid, {}).setdefault("newly_cleared", 0)
                out["by_rule"][rid]["newly_cleared"] += 1
        out["changed" if changed else "unchanged"] += 1
    return out


async def reevaluate(
    uow: UoW, p: Principal, case_ids: list[str] | None, all_open: bool, hub: Any, settings: Any
) -> dict[str, Any]:
    """Move cases to the latest published doc_requirements version and re-run completeness (records both versions)."""
    s = uow.session
    if all_open:
        ids = [
            str(r.id)
            for r in (
                await s.execute(
                    text(
                        "SELECT id FROM claim_case WHERE status IN ('draft','docs_pending','docs_complete')"
                    )
                )
            ).all()
        ]
    else:
        try:
            ids = [str(uuid.UUID(c)) for c in (case_ids or [])]
        except ValueError:
            raise ApiError("validation_error", "case_ids must be uuids") from None
    ver, _ = await comp.config_service.resolve(s, "doc_requirements")
    done = 0
    for cid in ids:
        old = (
            await s.execute(
                text("SELECT config_versions FROM claim_case WHERE id=:i FOR UPDATE"), {"i": cid}
            )
        ).scalar()
        if (
            old is None
            and not (
                await s.execute(text("SELECT 1 FROM claim_case WHERE id=:i"), {"i": cid})
            ).first()
        ):
            continue
        new = {**(old or {}), "doc_requirements": ver}
        await s.execute(
            text("UPDATE claim_case SET config_versions = CAST(:c AS jsonb) WHERE id=:i"),
            {"c": json.dumps(new), "i": cid},
        )
        await audit.append(
            s,
            cid,
            "case.updated",
            {
                "re_evaluated": True,
                "doc_requirements_from": (old or {}).get("doc_requirements"),
                "doc_requirements_to": ver,
            },
            actor_type="human",
            actor_id=p.actor_id,
            config_versions=new,
        )
        await uow.commit()
        await comp.run(uow, cid, "config_republish", hub=hub, settings=settings, force=True)
        done += 1
    return {"re_evaluated": done, "doc_requirements_version": ver}
