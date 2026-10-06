"""Draft read model, officer sign-off, submission assembly and the transactional outbox insert (doc 06 §4-5)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import uuid_utils
from claim_contract import models as cm
from claim_contract.errors import from_validation_error
from claim_contract.middleware import MAX_BODY
from claim_contract.outbox import canonical_bytes
from pydantic import ValidationError
from sqlalchemy import text

from app.auth.principal import Principal
from app.claim_validation.rules import required_acknowledgements, validate
from app.claim_validation.schemas import ClaimDraftIn
from app.completeness.service import invalidate_signoffs
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, config_service, transitions
from app.services.claim_builder import load_case_row, persist_draft, validation_context
from app.services.claim_edit import PatchOp, apply_patch

MAX_SUBMISSION_BYTES = 200 * 1024
WITHDRAWABLE = {"submitted", "acknowledged", "under_query"}


async def latest_draft(uow: UoW, case_id: Any) -> Any:
    row = (
        await uow.session.execute(
            text("SELECT * FROM claim_draft WHERE case_id=:c ORDER BY version DESC LIMIT 1"),
            {"c": case_id},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "no claim draft yet")
    return row


async def active_signoff(uow: UoW, draft_id: Any) -> Any:
    return (
        await uow.session.execute(
            text(
                "SELECT s.id, s.officer_id, s.comment, s.acknowledged_warnings, s.created_at FROM signoff s WHERE s.draft_id=:d "
                "AND s.decision='approved' AND s.invalidated_at IS NULL"
            ),
            {"d": draft_id},
        )
    ).first()


def draft_view(d: Any, signoff: Any, ready: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "case_id": str(d.case_id),
        "version": d.version,
        "source": d.source,
        "has_errors": d.has_errors,
        "payload": d.payload,
        "validation": d.validation,
        "provenance": d.provenance,
        "etag": f"draft-{d.version}",
        "created_by": d.created_by,
        "created_at": d.created_at.isoformat(),
        "signoff": None
        if signoff is None
        else {
            "id": str(signoff.id),
            "signed_by": str(signoff.officer_id),
            "acknowledged_warnings": signoff.acknowledged_warnings,
            "signed_at": signoff.created_at.isoformat(),
        },
        **({"ready_to_submit": ready} if ready is not None else {}),
    }


async def get_claim(uow: UoW, case: Any) -> dict[str, Any]:
    d = await latest_draft(uow, case.id)
    so = await active_signoff(uow, d.id)
    return draft_view(d, so, await ready_to_submit(uow, case, d, so))


async def versions(uow: UoW, case_id: Any) -> dict[str, Any]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT version, source, has_errors, created_by, created_at, edit_summary FROM claim_draft WHERE case_id=:c "
                "ORDER BY version DESC"
            ),
            {"c": case_id},
        )
    ).all()
    return {
        "versions": [
            {
                "version": r.version,
                "source": r.source,
                "has_errors": r.has_errors,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat(),
                "edit_summary": r.edit_summary,
            }
            for r in rows
        ]
    }


async def ready_to_submit(
    uow: UoW, case: Any, draft: Any | None = None, so: Any | None = None
) -> dict[str, Any]:
    reasons: list[str] = []
    if case.status_t != "ready_for_review":
        reasons.append(f"case_status_{case.status_t}")
    comp = (
        await uow.session.execute(
            text(
                "SELECT complete FROM completeness_check WHERE case_id=:c ORDER BY run_no DESC LIMIT 1"
            ),
            {"c": case.id},
        )
    ).first()
    if comp is None or not comp.complete:
        reasons.append("documents_incomplete")
    if draft is None:
        reasons.append("no_draft")
    else:
        if draft.has_errors:
            reasons.append("draft_has_errors")
        if so is None:
            reasons.append("signoff_missing_or_stale")
    route = case.route or {}
    needs = [w["code"] for w in route.get("warnings", []) if w.get("needs_ack")]
    if set(needs) - set((route.get("ack") or {}).get("codes", [])):
        reasons.append("route_warnings_unacknowledged")
    if (
        case.claim_type_t == "reimbursement"
        and case.filing_deadline
        and datetime.now(UTC).date() > case.filing_deadline
    ):
        reasons.append("filing_deadline_passed")
    return {"ok": not reasons, "reasons": reasons}


async def revalidate(uow: UoW, case: Any) -> dict[str, Any]:
    d = await latest_draft(uow, case.id)
    res = validate(ClaimDraftIn.model_validate(d.payload), await validation_context(uow, case))
    await uow.session.execute(
        text("UPDATE claim_draft SET validation=CAST(:v AS jsonb), has_errors=:e WHERE id=:i"),
        {"v": json.dumps(res.to_json()), "e": res.has_errors, "i": d.id},
    )
    await uow.commit()
    return {"version": d.version, **res.to_json(), "has_errors": res.has_errors}


async def edit(
    uow: UoW, p: Principal, case_id: Any, ops: list[PatchOp], if_match: str | None, hub: Any
) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t in (
        "submitted",
        "acknowledged",
        "under_query",
        "approved",
        "partially_approved",
        "rejected",
        "settled",
        "closed",
    ):
        raise ApiError(
            "case_locked",
            "the claim cannot be edited after submission; use the query response path",
            status=409,
        )
    if case.status_t != "ready_for_review":
        raise ApiError(
            "invalid_state",
            f"the draft can only be edited while ready for review (case is {case.status_t})",
        )
    d = await latest_draft(uow, case.id)
    if if_match is None or if_match.strip('"') != f"draft-{d.version}":
        raise ApiError(
            "precondition_failed",
            "If-Match must be the current draft etag",
            current=f"draft-{d.version}",
        )
    new_payload, changed = apply_patch(d.payload, ops)
    try:
        parsed = ClaimDraftIn.model_validate(new_payload)
    except ValidationError as e:
        raise from_validation_error(e) from e
    res = validate(parsed, await validation_context(uow, case))
    _, version = await persist_draft(
        uow,
        case,
        parsed,
        d.provenance,
        "human_edit",
        str(p.id),
        None,
        res,
        {"fields_changed": changed},
    )
    n = await invalidate_signoffs(uow, case.id, "draft edited")
    await audit.append(
        uow.session,
        case.id,
        "claim.edited",
        {"version": version, "fields_changed": changed},
        actor_type="human",
        actor_id=p.actor_id,
    )

    async def publish() -> None:
        await hub.publish("claim.draft_edited", str(case.id), {"version": version})
        if n:
            await hub.publish("claim.signoff_invalidated", str(case.id), {"reason": "draft edited"})

    uow.after_commit(publish)
    await uow.commit()
    return {
        "version": version,
        "etag": f"draft-{version}",
        "has_errors": res.has_errors,
        "fields_changed": changed,
    }


async def signoff(
    uow: UoW,
    p: Principal,
    case_id: Any,
    decision: str,
    comment: str | None,
    acknowledged: list[str],
    hub: Any,
    completeness: Any,
) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t != "ready_for_review":
        raise ApiError(
            "invalid_state",
            f"sign-off needs a case that is ready for review (case is {case.status_t})",
        )
    d = await latest_draft(uow, case.id)
    s = uow.session
    if decision == "returned":
        await s.execute(
            text(
                "INSERT INTO signoff (id, case_id, draft_id, officer_id, decision, comment) VALUES "
                "(uuid_generate_v7(), :c, :d, :o, 'returned', :m) ON CONFLICT ON CONSTRAINT uq_signoff_officer_draft DO NOTHING"
            ),
            {"c": case.id, "d": d.id, "o": p.id, "m": comment},
        )
        await audit.append(
            s,
            case.id,
            "claim.signoff",
            {"decision": "returned", "version": d.version},
            actor_type="human",
            actor_id=p.actor_id,
        )
        await transitions.transition(
            uow, case.id, "docs_pending", p, reason=comment or "returned by officer", hub=hub
        )
        await uow.commit()
        await completeness(str(case.id), "doc_event")
        return {"decision": "returned", "draft_version": d.version}
    gates_ver = (case.config_versions or {}).get("signoff")
    cfg = (
        await config_service.load_version(s, "signoff", int(gates_ver))
        if gates_ver is not None
        else (await config_service.resolve(s, "signoff"))[1]
    )
    if cfg.get("four_eyes") and d.created_by == str(p.id):
        raise ApiError("four_eyes_required", "a different officer must sign off a draft you edited")
    if d.has_errors:
        errs = [e["code"] for e in d.validation.get("errors", [])]
        raise ApiError(
            "draft_has_errors", "the draft has validation errors", errors=sorted(set(errs))
        )
    need = set(required_acknowledgements(d.validation))
    route = case.route or {}
    route_need = {w["code"] for w in route.get("warnings", []) if w.get("needs_ack")}
    missing = sorted(need - set(acknowledged))
    if missing:
        raise ApiError(
            "warnings_not_acknowledged", "every warning needs acknowledgement", missing=missing
        )
    unacked_route = sorted(route_need - set((route.get("ack") or {}).get("codes", [])))
    if unacked_route:
        raise ApiError(
            "warnings_not_acknowledged",
            "routing warnings need acknowledgement first (POST route/ack)",
            missing=unacked_route,
        )
    if await active_signoff(uow, d.id) is not None:
        raise ApiError("already_signed", "this draft version is already signed off")
    sid = (
        await s.execute(
            text(
                "INSERT INTO signoff (id, case_id, draft_id, officer_id, decision, comment, acknowledged_warnings) VALUES "
                "(uuid_generate_v7(), :c, :d, :o, 'approved', :m, :a) RETURNING id"
            ),
            {"c": case.id, "d": d.id, "o": p.id, "m": comment, "a": sorted(set(acknowledged))},
        )
    ).scalar()
    await audit.append(
        s,
        case.id,
        "human.signoff",
        {"role": "officer", "version": d.version, "acknowledged": sorted(set(acknowledged))},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"signoff_id": str(sid), "draft_version": d.version}


async def _refs(
    store: Any, docs: Any, ttl: int, received_via: str = "upload"
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for d in docs:
        if d.dtype is None:
            continue
        try:
            url = await store.presign_get(d.storage_key, ttl)
        except Exception:  # noqa: BLE001
            raise ApiError("storage_unavailable", "document storage is unavailable") from None
        refs.append(
            {
                "doc_id": str(d.id),
                "doc_type": d.dtype,
                "filename": d.original_filename[:200],
                "sha256": d.sha256,
                "size_bytes": d.size_bytes,
                "mime_type": d.mime_type,
                "download_url": url,
                "url_expires_at": (now + timedelta(seconds=ttl)).isoformat(),
                "parse_confidence": d.parse_confidence,
                "pages": d.pages or 1,
                "received_via": received_via,
            }
        )
    return refs


async def doc_refs_for(
    uow: UoW, store: Any, case: Any, doc_ids: list[str], ttl: int
) -> list[dict[str, Any]]:
    docs = (
        await uow.session.execute(
            text(
                "SELECT d.*, d.doc_type::text AS dtype FROM document d WHERE d.case_id=:c AND d.id = ANY(CAST(:ids AS uuid[])) AND "
                "d.lifecycle='active' AND d.scan_status='clean' AND d.storage_key IS NOT NULL ORDER BY d.created_at"
            ),
            {"c": case.id, "ids": doc_ids},
        )
    ).all()
    return await _refs(store, docs, ttl, "supplement")


async def assemble(
    uow: UoW, case: Any, draft: Any, store: Any, settings: Any, late_reason: str | None
) -> cm.ClaimSubmission:
    s = uow.session
    pat = (
        await s.execute(
            text("SELECT id_proof_hash FROM patient WHERE id=:i"), {"i": case.patient_id}
        )
    ).one()
    hosp = (
        await s.execute(text("SELECT code FROM hospital WHERE id=:i"), {"i": case.hospital_id})
    ).scalar()
    route = case.route or {}
    pipeline = (route.get("decision") or {}).get("pipeline") or case.claim_type_t
    docs = (
        await s.execute(
            text(
                "SELECT d.*, d.doc_type::text AS dtype FROM document d WHERE d.case_id=:c AND d.lifecycle='active' AND "
                "d.scan_status='clean' AND d.storage_key IS NOT NULL ORDER BY d.created_at"
            ),
            {"c": case.id},
        )
    ).all()
    now = datetime.now(UTC)
    refs = await _refs(store, docs, settings.presign_submit_ttl_s)
    pl = draft.payload
    lines = [
        {
            "line_id": f"L{ln['line_no']:03d}",
            "code": ln.get("code"),
            "description": ln["description"],
            "category": ln["category"],
            "qty": ln["qty"],
            "unit_price": {"amount": ln["unit_price"]},
            "amount": {"amount": ln["amount"]},
            "service_date": ln.get("service_date"),
            "source_doc_id": ln["source_doc_id"],
            "source_page": ln.get("source_page"),
        }
        for ln in pl["bill_lines"]
    ]
    cfg = {
        k: int(v)
        for k, v in (case.config_versions or {}).items()
        if k in ("doc_requirements", "deadlines", "router_rules", "confidence_gates")
    }
    journey = route.get("journey_id") or str(uuid.UUID(str(uuid_utils.uuid7())))
    notes = f"Late filing: {late_reason}" if late_reason else None
    body = {
        "contract_version": "1.1",
        "claim_ref": case.claim_ref,
        "claim_type": pipeline,
        "patient": {
            **{
                k: pl["patient"][k]
                for k in ("full_name", "dob", "gender", "member_id", "policy_number")
            },
            "id_proof_hash": pat.id_proof_hash,
        },
        "admission": {
            "admission_type": (route.get("decision") or {}).get("admission_type")
            or case.admission_type_t,
            "admitted_on": pl["admission"]["admitted_on"],
            "discharged_on": pl["admission"]["discharged_on"],
            "diagnosis_codes": pl["admission"]["diagnosis_codes"],
            "procedure_codes": pl["admission"]["procedure_codes"],
            "treating_doctor": pl["admission"]["treating_doctor"] or "Not recorded",
            "hospital_id": hosp,
            "preauth_ref": case.preauth_ref,
        },
        "bill_lines": lines,
        "totals": {
            "gross": {"amount": pl["totals"]["gross"]},
            "discounts": {"amount": pl["totals"]["discounts"]},
            "claimed": {"amount": pl["totals"]["claimed"]},
        },
        "documents": refs,
        "config_versions": cfg,
        "journey_id": journey,
        "submitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hospital_notes": notes,
    }
    try:
        return cm.ClaimSubmission.model_validate(body)
    except ValidationError as e:
        err = from_validation_error(e)
        raise ApiError(
            err.code,
            "the claim does not satisfy the insurer contract",
            errors=[x.model_dump() for x in err.errors],
        ) from None


async def submit(
    uow: UoW,
    p: Principal,
    case_id: Any,
    late_reason: str | None,
    store: Any,
    settings: Any,
    hub: Any,
) -> dict[str, Any]:
    s = uow.session
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t in (
        "submitted",
        "acknowledged",
        "under_query",
        "approved",
        "partially_approved",
        "rejected",
        "settled",
        "closed",
    ):
        raise ApiError(
            "already_submitted", f"the claim was already submitted (status {case.status_t})"
        )
    if case.status_t != "ready_for_review":
        raise ApiError(
            "invalid_state",
            f"only a case that is ready for review can be submitted (case is {case.status_t})",
        )
    d = await latest_draft(uow, case.id)
    so = await active_signoff(uow, d.id)
    if so is None:
        stale = (
            await s.execute(
                text(
                    "SELECT d2.version FROM signoff s JOIN claim_draft d2 ON d2.id = s.draft_id WHERE s.case_id=:c AND s.decision='approved' "
                    "AND s.invalidated_at IS NULL"
                ),
                {"c": case.id},
            )
        ).first()
        if stale is not None:
            raise ApiError(
                "signoff_stale",
                f"draft v{d.version} was created after sign-off on v{stale.version}",
            )
        raise ApiError(
            "signoff_required", "an officer sign-off on the latest draft version is required"
        )
    comp = (
        await s.execute(
            text(
                "SELECT complete FROM completeness_check WHERE case_id=:c ORDER BY run_no DESC LIMIT 1"
            ),
            {"c": case.id},
        )
    ).first()
    if comp is None or not comp.complete:
        raise ApiError("docs_incomplete", "completeness has outstanding items")
    fresh = validate(ClaimDraftIn.model_validate(d.payload), await validation_context(uow, case))
    if fresh.has_errors:
        raise ApiError(
            "draft_has_errors",
            "the draft no longer validates",
            errors=sorted({f.code for f in fresh.errors}),
        )
    route = case.route or {}
    unacked = sorted(
        {w["code"] for w in route.get("warnings", []) if w.get("needs_ack")}
        - set((route.get("ack") or {}).get("codes", []))
    )
    if unacked:
        raise ApiError(
            "warnings_not_acknowledged", "routing warnings need acknowledgement", missing=unacked
        )
    pipeline = (route.get("decision") or {}).get("pipeline") or case.claim_type_t
    if (
        pipeline == "reimbursement"
        and case.filing_deadline
        and datetime.now(UTC).date() > case.filing_deadline
        and not (late_reason or "").strip()
    ):
        raise ApiError(
            "filing_deadline_passed", "the filing deadline has passed; a reason is required"
        )
    sub = await assemble(uow, case, d, store, settings, (late_reason or "").strip() or None)
    body = sub.model_dump(mode="json", exclude_none=True)
    raw = canonical_bytes(body)
    if len(raw) > MAX_SUBMISSION_BYTES or len(raw) > MAX_BODY:
        raise ApiError("submission_too_large", "the serialised submission exceeds the size limit")
    attempt = int(
        (
            await s.execute(
                text("SELECT count(*) FROM outbox WHERE case_id=:c AND kind='claim.submit'"),
                {"c": case.id},
            )
        ).scalar()
    )
    idem = uuid.uuid5(uuid.NAMESPACE_URL, f"{case.claim_ref}:{d.version}:{attempt}")
    seq = int(
        (
            await s.execute(
                text("SELECT COALESCE(max(sequence), 0) + 1 FROM outbox WHERE case_id=:c"),
                {"c": case.id},
            )
        ).scalar()
    )
    oid = (
        await s.execute(
            text(
                "INSERT INTO outbox (id, case_id, kind, method, path, body, body_sha256, idempotency_key, sequence, status) VALUES "
                "(uuid_generate_v7(), :c, 'claim.submit', 'POST', '/v1/hospital-api/claims', CAST(:b AS jsonb), :h, :k, :n, 'pending') "
                "RETURNING id"
            ),
            {
                "c": case.id,
                "b": json.dumps(body),
                "h": hashlib.sha256(raw).hexdigest(),
                "k": idem,
                "n": seq,
            },
        )
    ).scalar()
    route["journey_id"] = body.get("journey_id")
    await s.execute(
        text("UPDATE claim_case SET route=CAST(:r AS jsonb), claimed_amount=:a WHERE id=:i"),
        {"r": json.dumps(route), "a": sub.totals.claimed.amount, "i": case.id},
    )
    await transitions.transition(
        uow, case.id, "submitted", p, reason="submitted by officer", hub=hub
    )
    await audit.append(
        s,
        case.id,
        "claim.submitted",
        {
            "claim_ref": case.claim_ref,
            "idempotency_key": str(idem),
            "contract_version": "1.1",
            "draft_version": d.version,
        },
        actor_type="human",
        actor_id=p.actor_id,
        config_versions=case.config_versions or {},
    )
    await uow.commit()
    return {
        "outbox_id": str(oid),
        "status": "pending",
        "idempotency_key": str(idem),
        "case_status": "submitted",
    }


async def submission_status(uow: UoW, case: Any) -> dict[str, Any]:
    row = (
        await uow.session.execute(
            text(
                "SELECT id, status, attempts, last_error, response_status, next_attempt_at, sent_at, kind FROM outbox "
                "WHERE case_id=:c AND kind='claim.submit' ORDER BY sequence DESC LIMIT 1"
            ),
            {"c": case.id},
        )
    ).first()
    d = None
    so = None
    try:
        d = await latest_draft(uow, case.id)
        so = await active_signoff(uow, d.id)
    except ApiError:
        pass
    last_error = None
    if row is not None and row.last_error:
        try:
            last_error = json.loads(row.last_error)
        except ValueError:
            last_error = {"message": row.last_error}
    return {
        "status": row.status if row else None,
        "outbox_id": str(row.id) if row else None,
        "attempts": row.attempts if row else 0,
        "last_error": last_error,
        "next_attempt_at": row.next_attempt_at.isoformat()
        if row and row.status == "pending"
        else None,
        "insurer_claim_no": case.insurer_claim_no,
        "acknowledged_at": case.acknowledged_at.isoformat() if case.acknowledged_at else None,
        "ready_to_submit": await ready_to_submit(uow, case, d, so),
    }


async def retry(uow: UoW, p: Principal, case_id: Any) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    row = (
        await uow.session.execute(
            text(
                "SELECT id FROM outbox WHERE case_id=:c AND status='dead' ORDER BY sequence LIMIT 1 FOR UPDATE"
            ),
            {"c": case.id},
        )
    ).first()
    if row is None:
        raise ApiError("invalid_state", "there is no dead-lettered delivery to retry")
    await uow.session.execute(
        text(
            "UPDATE outbox SET status='pending', attempts=0, next_attempt_at=now(), last_error=NULL WHERE id=:i"
        ),
        {"i": row.id},
    )  # same idempotency key: a late success at the insurer is not duplicated
    await audit.append(
        uow.session,
        case.id,
        "submission.retried",
        {"outbox_id": str(row.id)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"outbox_id": str(row.id), "status": "pending"}


async def reopen(uow: UoW, p: Principal, case_id: Any, hub: Any) -> dict[str, Any]:
    """The receiver rejected the submission outright (terminal 4xx): nothing was accepted, so the officer may fix and resubmit."""
    case = await load_case_row(uow, case_id, lock=True)
    row = (
        await uow.session.execute(
            text(
                "SELECT id FROM outbox WHERE case_id=:c AND kind='claim.submit' AND status='failed' ORDER BY sequence DESC LIMIT 1 FOR UPDATE"
            ),
            {"c": case.id},
        )
    ).first()
    if case.status_t != "submitted" or row is None:
        raise ApiError(
            "invalid_state", "only a submission rejected by the receiver can be reopened"
        )
    await transitions.transition(
        uow, case.id, "ready_for_review", p, reason="submission rejected by receiver", hub=hub
    )
    await invalidate_signoffs(uow, case.id, "submission rejected by receiver")
    await audit.append(
        uow.session,
        case.id,
        "submission.reopened",
        {"outbox_id": str(row.id)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"case_status": "ready_for_review"}


async def withdraw(
    uow: UoW, p: Principal, case_id: Any, reason: str, note: str | None
) -> dict[str, Any]:
    case = await load_case_row(uow, case_id, lock=True)
    if case.status_t not in WITHDRAWABLE:
        raise ApiError("invalid_state", f"a claim in status {case.status_t} cannot be withdrawn")
    s = uow.session
    if (
        await s.execute(
            text("SELECT 1 FROM outbox WHERE case_id=:c AND kind='claim.withdraw'"), {"c": case.id}
        )
    ).first():
        raise ApiError("already_submitted", "a withdrawal is already queued")
    body = {"reason": reason, "note": note} if note else {"reason": reason}
    raw = canonical_bytes(body)
    seq = int(
        (
            await s.execute(
                text("SELECT COALESCE(max(sequence), 0) + 1 FROM outbox WHERE case_id=:c"),
                {"c": case.id},
            )
        ).scalar()
    )
    await s.execute(
        text(
            "INSERT INTO outbox (id, case_id, kind, method, path, body, body_sha256, idempotency_key, sequence, status) VALUES "
            "(uuid_generate_v7(), :c, 'claim.withdraw', 'POST', :path, CAST(:b AS jsonb), :h, :k, :n, 'pending')"
        ),
        {
            "c": case.id,
            "path": f"/v1/hospital-api/claims/{case.claim_ref}/withdraw",
            "b": json.dumps(body),
            "h": hashlib.sha256(raw).hexdigest(),
            "k": uuid.uuid5(uuid.NAMESPACE_URL, f"{case.claim_ref}:withdraw"),
            "n": seq,
        },
    )
    await audit.append(
        s, case.id, "claim.withdrawn", {"reason": reason}, actor_type="human", actor_id=p.actor_id
    )
    await uow.commit()
    return {"status": "pending", "case_status": case.status_t}
