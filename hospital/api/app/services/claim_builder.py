"""Claim Builder trigger and draft intake (doc 06 §5.1-5.3). The agent proposes; deterministic code disposes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.auth.principal import Principal
from app.claim_validation.rules import (
    CaseFacts,
    DocInfo,
    ValidationContext,
    ValidationResult,
    validate,
)
from app.claim_validation.schemas import ClaimDraftIn, DraftResult
from app.completeness.rules import field_value, parse_amount
from app.completeness.service import invalidate_signoffs
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, config_service, transitions

MAX_REPAIR_ROUNDS = 2


def _job_key(case_id: Any) -> str:
    return f"cache:hosp:claimjob:{case_id}"


async def load_case_row(uow: UoW, case_id: Any, lock: bool = False) -> Any:
    sql = (
        "SELECT c.*, c.status::text AS status_t, c.claim_type::text AS claim_type_t, "
        "c.admission_type::text AS admission_type_t FROM claim_case c WHERE c.id=:i"
    )
    row = (
        await uow.session.execute(
            text(sql + (" FOR NO KEY UPDATE" if lock else "")), {"i": uuid.UUID(str(case_id))}
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown case")
    return row


async def validation_context(uow: UoW, case: Any) -> ValidationContext:
    s = uow.session
    pat = (
        await s.execute(
            text("SELECT full_name, dob FROM patient WHERE id=:i"), {"i": case.patient_id}
        )
    ).one()
    pre = None
    if case.preauth_ref:
        pre = (
            await s.execute(
                text("SELECT approved_amount FROM simulated_preauth WHERE ref=:r"),
                {"r": case.preauth_ref},
            )
        ).scalar()
    pipeline = ((case.route or {}).get("decision") or {}).get("pipeline") or case.claim_type_t
    rows = (
        await s.execute(
            text(
                "SELECT d.id, d.pages, d.scan_status, d.lifecycle, d.doc_type::text AS dtype, "
                "(SELECT jsonb_object_agg(k, v) FROM (SELECT (jsonb_each(p.typed_json)).* FROM document_parse p "
                "WHERE p.document_id = d.id ORDER BY p.pass_no) x(k, v) WHERE jsonb_typeof(v) <> 'null') AS j FROM document d WHERE d.case_id=:c"
            ),
            {"c": case.id},
        )
    ).all()
    docs: dict[str, DocInfo] = {}
    for r in rows:
        total = None
        if r.j:
            from app.completeness.context import DocFacts

            total = parse_amount(
                field_value(
                    DocFacts(str(r.id), r.dtype or "", "ok", datetime.now(UTC), typed_json=r.j),
                    "total",
                )
            )
        docs[str(r.id)] = DocInfo(
            str(r.id),
            r.pages or 1,
            r.lifecycle == "active" and r.scan_status == "clean",
            r.dtype,
            total,
        )
    cfg_v = (case.config_versions or {}).get(
        "doc_requirements"
    )  # sanity ceilings come from the same config family
    del cfg_v
    return ValidationContext(
        CaseFacts(
            case.claim_ref,
            pipeline,
            pat.full_name,
            pat.dob,
            case.admitted_on,
            case.discharged_on,
            Decimal(pre) if pre is not None else None,
        ),
        docs,
    )


async def next_version(uow: UoW, case_id: Any) -> int:
    return int(
        (
            await uow.session.execute(
                text("SELECT COALESCE(max(version), 0) + 1 FROM claim_draft WHERE case_id=:c"),
                {"c": case_id},
            )
        ).scalar_one()
    )


async def persist_draft(
    uow: UoW,
    case: Any,
    payload: ClaimDraftIn,
    provenance: dict[str, Any],
    source: str,
    created_by: str,
    model_info: dict[str, Any] | None,
    result: ValidationResult,
    edit_summary: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, int]:
    s = uow.session
    version = await next_version(uow, case.id)
    body = payload.model_dump(mode="json")
    draft_id = (
        await s.execute(
            text(
                "INSERT INTO claim_draft (id, case_id, version, payload, validation, source, created_by, provenance, has_errors, "
                "model_info, edit_summary) VALUES (uuid_generate_v7(), :c, :v, CAST(:p AS jsonb), CAST(:val AS jsonb), :src, :by, "
                "CAST(:prov AS jsonb), :err, CAST(:mi AS jsonb), CAST(:es AS jsonb)) RETURNING id"
            ),
            {
                "c": case.id,
                "v": version,
                "p": json.dumps(body),
                "val": json.dumps(result.to_json()),
                "src": source,
                "by": created_by,
                "prov": json.dumps(provenance),
                "err": result.has_errors,
                "mi": json.dumps(model_info) if model_info else None,
                "es": json.dumps(edit_summary) if edit_summary else None,
            },
        )
    ).scalar_one()  # RETURNING id
    for ln in payload.bill_lines:
        await s.execute(
            text(
                "INSERT INTO bill_line (id, draft_id, line_no, code, description, category, qty, unit_price, amount, "
                "source_document_id, source_page) VALUES (uuid_generate_v7(), :d, :n, :code, :desc, :cat, :q, :u, :a, :doc, :pg)"
            ),
            {
                "d": draft_id,
                "n": ln.line_no,
                "code": ln.code,
                "desc": ln.description,
                "cat": ln.category.value,
                "q": ln.qty,
                "u": ln.unit_price,
                "a": ln.amount,
                "doc": ln.source_doc_id,
                "pg": ln.source_page,
            },
        )
    return draft_id, version


async def start(
    uow: UoW,
    p: Principal,
    case_id: Any,
    crew: Any,
    redis: Any,
    hub: Any,
    settings: Any,
    completeness: Any = None,
) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t != "docs_complete":
        blockers = ""
        row = (
            await uow.session.execute(
                text(
                    "SELECT blocker_count FROM completeness_check WHERE case_id=:c ORDER BY run_no DESC LIMIT 1"
                ),
                {"c": case.id},
            )
        ).first()
        if row is not None and row.blocker_count:
            blockers = f"; completeness has {row.blocker_count} blockers"
        raise ApiError("invalid_transition", f"case is {case.status_t}{blockers}")
    day = datetime.now(UTC).strftime("%Y%m%d")
    n = await redis.incr(f"cache:hosp:claimjob:seq:{day}")
    await redis.expire(f"cache:hosp:claimjob:seq:{day}", 172800)
    job_id = f"cb-{day}-{n:04d}"
    docs = [
        str(r.id)
        for r in (
            await uow.session.execute(
                text(
                    "SELECT id FROM document WHERE case_id=:c AND lifecycle='active' AND scan_status='clean' ORDER BY created_at"
                ),
                {"c": case.id},
            )
        ).all()
    ]
    await transitions.transition(
        uow, case.id, "building_claim", p, reason="claim build requested", hub=hub
    )
    await audit.append(
        uow.session,
        case.id,
        "claim.build_started",
        {"job_id": job_id, "documents": len(docs)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    job = {
        "job_id": job_id,
        "case_id": str(case.id),
        "document_ids": docs,
        "route": (case.route or {}).get("decision"),
        "config_versions": case.config_versions,
        "repair": None,
        "callback": f"/v1/internal/cases/{case.id}/claim/draft",
    }
    await redis.set(
        _job_key(case.id),
        json.dumps({"job_id": job_id, "round": 0}),
        ex=settings.claim_build_timeout_s,
    )
    if not await crew.start_claim_build(job):
        await fail_build(
            uow, case.id, "claim builder is unavailable", hub=hub, completeness=completeness
        )
        raise ApiError(
            "crew_unavailable", "the claim builder service is unavailable; try again shortly"
        )
    return {"job_id": job_id, "status": "queued", "case_status": "building_claim"}


async def fail_build(
    uow: UoW, case_id: Any, reason: str, *, hub: Any = None, completeness: Any = None
) -> None:
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t != "building_claim":
        await uow.rollback()
        return
    await transitions.transition(
        uow, case.id, "docs_pending", None, reason=f"build failed: {reason}", hub=hub
    )
    await audit.append(
        uow.session, case.id, "claim.build_failed", {"reason": reason}, actor_id="claim-builder"
    )
    await uow.commit()
    if completeness is not None:
        await completeness(
            str(case.id), "doc_event"
        )  # docs are still complete: the loop returns the case to docs_complete


async def accept_draft(
    uow: UoW,
    svc: Principal,
    case_id: Any,
    result: DraftResult,
    *,
    crew: Any,
    redis: Any,
    hub: Any,
    completeness: Any,
    settings: Any,
) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    s = uow.session
    dup = (
        await s.execute(
            text(
                "SELECT version, has_errors FROM claim_draft WHERE case_id=:c AND model_info->>'job_id' = :j AND "
                "COALESCE((model_info->>'repair_round')::int, 0) = :r"
            ),
            {"c": case.id, "j": result.job_id, "r": result.repair_round},
        )
    ).first()
    if dup is not None:
        await uow.rollback()
        return {"version": dup.version, "has_errors": dup.has_errors, "duplicate": True}
    if case.status_t != "building_claim":
        raise ApiError("invalid_state", f"case is {case.status_t}; no claim build is in progress")
    ctx = await validation_context(uow, case)
    res = validate(result.payload, ctx)
    source = "repair" if result.repair_round > 0 else "agent"
    mi = {**result.model_info, "job_id": result.job_id, "repair_round": result.repair_round}
    draft_id, version = await persist_draft(
        uow,
        case,
        result.payload,
        {k: v.model_dump(mode="json") for k, v in result.provenance.items()},
        source,
        "agent:claim-builder",
        mi,
        res,
    )
    await invalidate_signoffs(uow, case.id, "new agent draft")
    repair = res.has_errors and result.repair_round < MAX_REPAIR_ROUNDS
    if repair:
        await audit.append(
            s,
            case.id,
            "claim.built",
            {"version": version, "errors": len(res.errors), "repair_pending": True},
            actor_id="claim-builder",
            model_info=mi,
        )
        await uow.commit()
        job = {
            "job_id": result.job_id,
            "case_id": str(case.id),
            "document_ids": [str(d) for d in result.payload.documents],
            "route": (case.route or {}).get("decision"),
            "config_versions": case.config_versions,
            "callback": f"/v1/internal/cases/{case.id}/claim/draft",
            "repair": {
                "round": result.repair_round + 1,
                "errors": [
                    {"code": f.code, "field": f.field, "message": f.message} for f in res.errors
                ],
                "previous_draft_version": version,
            },
        }
        await redis.set(
            _job_key(case.id),
            json.dumps({"job_id": result.job_id, "round": result.repair_round + 1}),
            ex=settings.claim_build_timeout_s,
        )
        if not await crew.start_claim_build(job):
            await fail_build(
                uow,
                case.id,
                "repair request could not be delivered",
                hub=hub,
                completeness=completeness,
            )
        return {"version": version, "has_errors": True, "repair_requested": result.repair_round + 1}
    await transitions.transition(
        uow, case.id, "ready_for_review", None, reason="claim draft ready", hub=hub
    )
    await audit.append(
        s,
        case.id,
        "claim.built",
        {"version": version, "errors": len(res.errors), "warnings": len(res.warnings)},
        actor_id="claim-builder",
        model_info=mi,
    )

    async def publish() -> None:
        await hub.publish(
            "claim.draft_ready", str(case.id), {"version": version, "has_errors": res.has_errors}
        )

    uow.after_commit(publish)
    await uow.commit()
    await redis.delete(_job_key(case.id))
    return {"version": version, "has_errors": res.has_errors, "draft_id": str(draft_id)}


async def expire_builds(uow: UoW, redis: Any, hub: Any, completeness: Any) -> dict[str, int]:
    """Cases stuck in building_claim whose job key has expired go back (doc 06 §8.1)."""
    ids = [
        r.id
        for r in (
            await uow.session.execute(
                text("SELECT id FROM claim_case WHERE status='building_claim'")
            )
        ).all()
    ]
    n = 0
    for cid in ids:
        if await redis.exists(_job_key(cid)):
            continue
        await fail_build(uow, cid, "timed out", hub=hub, completeness=completeness)
        n += 1
    return {"timed_out": n}


__all__ = ["config_service"]
