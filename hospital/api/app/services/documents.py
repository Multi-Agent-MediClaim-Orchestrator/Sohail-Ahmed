"""Document intake: validate, scan, store, de-duplicate, supersede, delete, parse callbacks, jobs."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.deps import case_scope_sql
from app.auth.principal import Principal
from app.completeness.service import invalidate_signoffs
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, config_service, filechecks, parse_logic, transitions
from app.services.cases import load_case
from app.storage.clamav import ClamAV
from app.storage.minio import ObjectStore

UPLOADABLE = {"draft", "docs_pending", "docs_complete", "ready_for_review"}
POST_SUBMIT = {
    "submitted",
    "acknowledged",
    "under_query",
    "approved",
    "partially_approved",
    "rejected",
    "settled",
    "closed",
}
DELETABLE = {"draft", "docs_pending", "docs_complete", "ready_for_review"}


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if isinstance(v, uuid.UUID) else v)


def doc_view(r: Any, uploader: str | None = None) -> dict[str, Any]:
    usable = (
        r.lifecycle == "active"
        and r.scan_status == "clean"
        and "illegible_candidate" not in (r.quality_flags or [])
    )
    needs = r.parse_status in ("needs_review", "failed") or r.doc_type is None
    return {
        "id": str(r.id),
        "case_id": str(r.case_id),
        "filename": r.original_filename,
        "mime_type": r.mime_type,
        "size_bytes": r.size_bytes,
        "pages": r.pages,
        "sha256": r.sha256,
        "scan_status": r.scan_status,
        "lifecycle": r.lifecycle,
        "doc_type": r.doc_type_t if hasattr(r, "doc_type_t") else r.doc_type,
        "doc_type_source": r.doc_type_source,
        "classification_confidence": r.classification_confidence,
        "parse_status": r.parse_status,
        "parse_confidence": r.parse_confidence,
        "quality": {
            "score": r.quality_score,
            "flags": r.quality_flags,
            "has_required_stamp": r.has_required_stamp,
        },
        "supersedes_id": _iso(r.supersedes_id),
        "parent_id": _iso(r.parent_id),
        "uploaded_by": {"id": _iso(r.uploaded_by), "name": uploader} if r.uploaded_by else None,
        "created_at": _iso(r.created_at),
        "usable": usable,
        "needs_attention": needs,
    }


DOC_SELECT = (
    "SELECT d.*, d.doc_type::text AS doc_type_t, u.display_name AS uploader_name FROM document d "
    "LEFT JOIN app_user u ON u.id = d.uploaded_by"
)


async def get_doc_row(uow: UoW, p: Principal, doc_id: str) -> Any:
    try:
        did = uuid.UUID(doc_id)
    except ValueError:
        raise ApiError("not_found", "unknown document") from None
    scope_sql, params = case_scope_sql(p, "c")
    row = (
        await uow.session.execute(
            text(
                f"{DOC_SELECT} JOIN claim_case c ON c.id = d.case_id WHERE d.id = :d AND {scope_sql}"
            ),  # noqa: S608
            {"d": did, **params},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown document")
    return row


async def list_docs(
    uow: UoW,
    p: Principal,
    case_id: str,
    lifecycle: str,
    doc_type: str | None,
    needs_attention: bool | None,
) -> dict[str, Any]:
    c = await load_case(uow, p, case_id)
    where, params = ["d.case_id = :c"], {"c": c.id}
    if lifecycle == "active":
        where.append("d.lifecycle = 'active'")
    if doc_type:
        where.append("d.doc_type = CAST(:dt AS doc_type)")
        params["dt"] = doc_type
    if needs_attention is not None:
        cond = "(d.parse_status IN ('needs_review','failed') OR d.doc_type IS NULL)"
        where.append(cond if needs_attention else f"NOT {cond}")
    rows = (
        await uow.session.execute(
            text(f"{DOC_SELECT} WHERE " + " AND ".join(where) + " ORDER BY d.created_at"),  # noqa: S608
            params,
        )
    ).all()
    return {"documents": [doc_view(r, r.uploader_name) for r in rows]}


async def _gates(uow: UoW) -> dict[str, Any]:
    return (await config_service.resolve(uow.session, "confidence_gates"))[1]


# ---------------------------------------------------------------------------------------- ingest
class Ingest:
    """Per-request dependencies for the upload pipeline."""

    def __init__(
        self,
        settings: Settings,
        store: ObjectStore,
        clam: ClamAV,
        n8n: Any,
        hub: Any,
        redis: Any,
        completeness: Any,
    ) -> None:
        self.s, self.store, self.clam, self.n8n, self.hub, self.redis = (
            settings,
            store,
            clam,
            n8n,
            hub,
            redis,
        )
        self.completeness = completeness


async def rate_limit(d: Ingest, p: Principal) -> None:
    import time

    k = f"rl:hosp:upload:{p.actor_id}:{int(time.time() // 60)}"
    n = await d.redis.incr(k)
    if n == 1:
        await d.redis.expire(k, 120)
    if n > d.s.upload_rate_per_min:
        raise ApiError("rate_limited", "upload rate limit exceeded", headers={"Retry-After": "30"})


async def ingest(
    uow: UoW,
    d: Ingest,
    case: Any,
    p: Principal,
    filename: str,
    data: bytes,
    *,
    hint: str | None = None,
    supersedes_id: str | None = None,
    purpose: str = "upload",
) -> dict[str, Any]:
    """Process one file. Returns a result dict (accepted/skipped_duplicate/rejected); never raises for
    per-file problems, only for request-level ones (scanner down, case locked, quota)."""
    status = case.status_t
    if status == "building_claim" or (status in POST_SUBMIT and purpose != "query"):
        raise ApiError("case_locked", f"documents cannot be added while the case is {status}")
    safe_name = filechecks.sanitize_filename(filename)
    try:
        return await _ingest_one(uow, d, case, p, safe_name, data, hint, supersedes_id, purpose)
    except ApiError as e:
        if e.code in ("scan_unavailable", "case_locked", "rate_limited", "too_many_files"):
            raise
        await uow.rollback()
        return {
            "filename": safe_name,
            "status": "rejected",
            "error": {"code": e.code, "detail": e.detail},
        }


async def _ingest_one(
    uow: UoW,
    d: Ingest,
    case: Any,
    p: Principal,
    name: str,
    data: bytes,
    hint: str | None,
    supersedes_id: str | None,
    purpose: str,
) -> dict[str, Any]:
    s = uow.session
    if len(data) == 0:
        raise ApiError("empty_file", "the file is empty")
    if len(data) > d.s.max_upload_bytes:
        raise ApiError("file_too_large", f"file exceeds {d.s.max_upload_mb} MB")
    orig_sha = hashlib.sha256(data).hexdigest()
    existing = await _by_hash(uow, case.id, orig_sha)
    if existing is not None:
        return _dup_result(name, existing)
    count = (
        await s.execute(
            text("SELECT count(*) FROM document WHERE case_id=:c AND lifecycle='active'"),
            {"c": case.id},
        )
    ).scalar()
    if count >= d.s.max_files_per_case:
        raise ApiError(
            "too_many_files", f"case already has {count} documents (max {d.s.max_files_per_case})"
        )

    doc_id = uuid.uuid4()
    scan = await d.clam.scan(data)  # raises scan_unavailable (fail closed)
    if not scan.clean:
        if scan.encrypted:
            raise ApiError(
                "encrypted_pdf", "encrypted or password-protected file; upload an unlocked copy"
            )
        return await _quarantine(
            uow,
            d,
            case,
            p,
            name,
            data,
            filechecks.sniff(data[:16]) or "application/octet-stream",
            orig_sha,
            doc_id,
            scan.signature or "unknown",
        )
    # type check AFTER the scan: infected files are quarantined whatever they claim to be
    mime = filechecks.sniff(data[:16])
    if mime is None or not filechecks.ext_matches(mime, name):
        raise ApiError("unsupported_media_type", mime or "unrecognised file type")

    pages = 1
    if mime == "application/pdf":
        pages = filechecks.check_pdf(data, d.s.max_pdf_pages)
    else:
        data, pages = filechecks.normalise_image(data, mime)
    sha = hashlib.sha256(data).hexdigest()
    if sha != orig_sha and (ex := await _by_hash(uow, case.id, sha)) is not None:
        return _dup_result(name, ex)

    sup_uuid = None
    if supersedes_id:
        try:
            sup_uuid = uuid.UUID(supersedes_id)
        except ValueError:
            raise ApiError("invalid_supersede", "supersedes_id must be a uuid") from None
        old = (
            await s.execute(
                text("SELECT lifecycle, case_id FROM document WHERE id=:i FOR UPDATE"),
                {"i": sup_uuid},
            )
        ).first()
        if old is None or old.case_id != case.id or old.lifecycle != "active":
            raise ApiError(
                "invalid_supersede", "can only supersede an active document of the same case"
            )

    key = f"{case.id}/{doc_id}/original.{filechecks.EXT_FOR[mime]}"
    await d.store.put(key, data, mime, {"sha256": sha, "origin-system": "hospital"})
    try:
        await s.execute(
            text(
                "INSERT INTO document (id, case_id, original_filename, mime_type, size_bytes, sha256, storage_key, pages, "
                "scan_status, doc_type, doc_type_source, supersedes_id, uploaded_by) VALUES (:id, :c, :n, :m, :sz, :sha, "
                ":k, :pg, 'clean', CAST(:dt AS doc_type), :src, :sup, :u)"
            ),
            {
                "id": doc_id,
                "c": case.id,
                "n": name,
                "m": mime,
                "sz": len(data),
                "sha": sha,
                "k": key,
                "pg": pages,
                "dt": hint,
                "src": "manual" if hint else None,
                "sup": sup_uuid,
                "u": p.id,
            },
        )
        if sup_uuid:
            await s.execute(
                text("UPDATE document SET lifecycle='superseded' WHERE id=:i"), {"i": sup_uuid}
            )
            await audit.append(
                s,
                case.id,
                "doc.superseded",
                {"doc_id": str(sup_uuid), "by": str(doc_id)},
                actor_type="human",
                actor_id=p.actor_id,
            )
        await audit.append(
            s,
            case.id,
            "doc.uploaded",
            {"doc_id": str(doc_id), "mime": mime, "size": len(data)},
            actor_type="human",
            actor_id=p.actor_id,
        )
        await audit.append(s, case.id, "doc.scanned", {"doc_id": str(doc_id), "result": "clean"})
        new_status = {
            "draft": "docs_pending",
            "docs_complete": "docs_pending",
            "ready_for_review": "docs_pending",
        }.get(status_of(case))
        if new_status and purpose != "query":
            await transitions.transition(
                uow, case.id, new_status, p, reason="document uploaded", hub=d.hub
            )
            if status_of(case) == "ready_for_review":
                await invalidate_signoffs(uow, case.id, "document uploaded")
        await s.execute(
            text("UPDATE document SET last_trigger_at = now() WHERE id=:i"), {"i": doc_id}
        )
    except IntegrityError:
        await uow.rollback()
        await d.store.delete_quietly(key)
        ex = await _by_hash(uow, case.id, sha)
        if ex is not None:
            return _dup_result(name, ex)
        raise

    async def after() -> None:
        await d.hub.publish(
            "document.uploaded", str(case.id), {"document_id": str(doc_id), "filename": name}
        )
        await d.hub.publish(
            "document.scanned", str(case.id), {"document_id": str(doc_id), "result": "clean"}
        )
        await d.n8n.trigger(
            "intake/document-uploaded",
            {"case_id": str(case.id), "document_id": str(doc_id)},
            str(doc_id),
        )

    uow.after_commit(after)
    await uow.commit()
    if supersedes_id or status_of(case) in ("docs_complete", "ready_for_review"):
        await d.completeness(str(case.id), "doc_event")
    return {
        "id": str(doc_id),
        "filename": name,
        "status": "accepted",
        "scan_status": "clean",
        "parse_status": "pending",
    }


def status_of(case: Any) -> str:
    return str(case.status_t)


async def _by_hash(uow: UoW, case_id: Any, sha: str) -> Any:
    return (
        await uow.session.execute(
            text(
                "SELECT id, lifecycle, scan_status FROM document WHERE case_id=:c AND sha256=:h AND lifecycle <> 'deleted'"
            ),
            {"c": case_id, "h": sha},
        )
    ).first()


def _dup_result(name: str, existing: Any) -> dict[str, Any]:
    if existing.lifecycle == "quarantined":
        return {
            "filename": name,
            "status": "rejected",
            "error": {"code": "infected", "detail": "previously quarantined"},
        }
    return {"filename": name, "status": "skipped_duplicate", "duplicate_of": str(existing.id)}


async def _quarantine(
    uow: UoW,
    d: Ingest,
    case: Any,
    p: Principal,
    name: str,
    data: bytes,
    mime: str,
    sha: str,
    doc_id: uuid.UUID,
    signature: str,
) -> dict[str, Any]:
    key = f"quarantine/{case.id}/{doc_id}.bin"
    await d.store.put(key, data, "application/octet-stream")
    s = uow.session
    await s.execute(
        text(
            "INSERT INTO document (id, case_id, original_filename, mime_type, size_bytes, sha256, storage_key, scan_status, "
            "lifecycle, quarantine_reason, uploaded_by, parse_status) VALUES (:id, :c, :n, :m, :sz, :sha, :k, 'infected', "
            "'quarantined', :q, :u, 'failed')"
        ),
        {
            "id": doc_id,
            "c": case.id,
            "n": name,
            "m": mime,
            "sz": len(data),
            "sha": sha,
            "k": key,
            "q": signature[:200],
            "u": p.id,
        },
    )
    await audit.append(
        s,
        case.id,
        "doc.scanned",
        {"doc_id": str(doc_id), "result": "infected", "signature": signature},
        actor_type="human",
        actor_id=p.actor_id,
    )

    async def alert() -> None:
        await d.hub.publish(
            "document.scanned", str(case.id), {"document_id": str(doc_id), "result": "infected"}
        )

    uow.after_commit(alert)
    await uow.commit()
    return {
        "filename": name,
        "status": "rejected",
        "error": {"code": "infected", "signature": signature},
    }


# -------------------------------------------------------------------------- user-facing mutations
async def reclassify(
    uow: UoW, d: Ingest, p: Principal, doc_id: str, new_type: str
) -> dict[str, Any]:
    row = await get_doc_row(uow, p, doc_id)
    case = await load_case(uow, p, str(row.case_id))
    if (
        row.lifecycle != "active"
        or case.status_t in POST_SUBMIT
        or case.status_t == "building_claim"
    ):
        raise ApiError("invalid_state", "document cannot be reclassified now")
    await uow.session.execute(
        text(
            "UPDATE document SET doc_type = CAST(:t AS doc_type), doc_type_source = 'manual' WHERE id = :i"
        ),
        {"t": new_type, "i": row.id},
    )
    await audit.append(
        uow.session,
        row.case_id,
        "doc.reclassified",
        {"doc_id": str(row.id), "old": row.doc_type_t, "new": new_type},
        actor_type="human",
        actor_id=p.actor_id,
    )

    async def after() -> None:
        await d.hub.publish(
            "document.classified",
            str(row.case_id),
            {"document_id": str(row.id), "doc_type": new_type},
        )

    uow.after_commit(after)
    await uow.commit()
    await d.completeness(str(row.case_id), "reclassify")
    return doc_view(await get_doc_row(uow, p, doc_id), row.uploader_name)


async def delete_doc(uow: UoW, d: Ingest, p: Principal, doc_id: str) -> None:
    row = await get_doc_row(uow, p, doc_id)
    case = await load_case(uow, p, str(row.case_id), for_update=True)
    if case.status_t in POST_SUBMIT:
        raise ApiError("case_submitted", "documents cannot be deleted after submission")
    if case.status_t == "building_claim":
        raise ApiError(
            "invalid_state", "documents cannot be deleted while the claim is being built"
        )
    if row.lifecycle not in ("active", "superseded"):
        raise ApiError("document_not_active", "document is not active")
    s = uow.session
    tomb = f"tombstone/{row.case_id}/{row.id}/{row.storage_key.rsplit('/', 1)[-1]}"
    await d.store.move(row.storage_key, tomb)
    await s.execute(
        text(
            "UPDATE document SET lifecycle='deleted', deleted_at=now(), deleted_by=:u, "
            "purge_after=now() + interval '30 days', storage_key=:k WHERE id=:i"
        ),
        {"u": p.id, "k": tomb, "i": row.id},
    )
    await audit.append(
        s,
        row.case_id,
        "doc.deleted",
        {"doc_id": str(row.id)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    if case.status_t == "ready_for_review":
        await transitions.transition(
            uow, row.case_id, "docs_pending", p, reason="document deleted", hub=d.hub
        )
        await invalidate_signoffs(uow, row.case_id, "document deleted")
    await uow.commit()
    await d.completeness(str(row.case_id), "doc_event")


async def reparse(uow: UoW, d: Ingest, p: Principal, doc_id: str, full: bool) -> dict[str, Any]:
    row = await get_doc_row(uow, p, doc_id)
    if row.lifecycle != "active":
        raise ApiError("document_not_active", "document is not active")
    if row.parse_status == "processing":
        raise ApiError("invalid_state", "parsing is already in progress")
    s = uow.session
    await s.execute(
        text("DELETE FROM document_parse WHERE document_id=:i AND (:full OR pass_no > 1)"),
        {"i": row.id, "full": full},
    )
    await s.execute(
        text(
            "UPDATE document SET parse_status='pending', parse_attempts=0, last_trigger_at=now(), "
            "parse_confidence=NULL WHERE id=:i"
        ),
        {"i": row.id},
    )
    await audit.append(
        s,
        row.case_id,
        "doc.reparse",
        {"doc_id": str(row.id), "full": full},
        actor_type="human",
        actor_id=p.actor_id,
    )

    async def after() -> None:
        await d.n8n.trigger(
            "intake/document-uploaded",
            {"case_id": str(row.case_id), "document_id": str(row.id)},
            f"{row.id}:reparse:{datetime.now().timestamp()}",
        )

    uow.after_commit(after)
    await uow.commit()
    return {"id": str(row.id), "parse_status": "pending"}


async def parse_detail(uow: UoW, p: Principal, doc_id: str) -> dict[str, Any]:
    row = await get_doc_row(uow, p, doc_id)
    passes = (
        await uow.session.execute(
            text(
                "SELECT pass_no, engine, engine_version, confidence, agreement_score, typed_json, duration_ms "
                "FROM document_parse WHERE document_id=:i ORDER BY pass_no"
            ),
            {"i": row.id},
        )
    ).all()
    agree = next((x.agreement_score for x in passes if x.agreement_score is not None), None)
    disagree: list[str] = []
    if len(passes) >= 2:
        disagree = parse_logic.disagreeing_fields(
            passes[0].typed_json or {}, passes[1].typed_json or {}, row.doc_type_t
        )
    return {
        "passes": [
            {
                "pass_no": x.pass_no,
                "engine": x.engine,
                "engine_version": x.engine_version,
                "confidence": x.confidence,
                "typed_json": x.typed_json,
                "duration_ms": x.duration_ms,
            }
            for x in passes
        ],
        "agreement_score": agree,
        "fields_disagreeing": disagree,
    }


async def presign(
    uow: UoW, d: Ingest, p: Principal, doc_id: str, key_override: str | None = None
) -> str:
    row = await get_doc_row(uow, p, doc_id)
    if row.lifecycle == "quarantined":
        raise ApiError("not_found", "unknown document")
    if row.lifecycle == "deleted":
        raise ApiError("gone", "document was deleted")
    if row.scan_status != "clean" or not row.storage_key:
        raise ApiError("not_found", "document not available")
    key = key_override or row.storage_key
    if key_override and not await d.store.exists(key):
        raise ApiError("not_found", "preview not available")
    await audit.append(
        uow.session,
        row.case_id,
        "doc.downloaded",
        {"doc_id": str(row.id)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return await d.store.presign_get(
        key, d.s.presign_ttl_s, filechecks.sanitize_filename(row.original_filename)
    )


# ----------------------------------------------------------------------------- internal callbacks
async def _active_doc(uow: UoW, doc_id: str, *, lock: bool = True) -> Any:
    try:
        did = uuid.UUID(doc_id)
    except ValueError:
        raise ApiError("not_found", "unknown document") from None
    row = (
        await uow.session.execute(
            text(
                "SELECT d.*, d.doc_type::text AS doc_type_t FROM document d WHERE d.id=:i"
                + (" FOR UPDATE" if lock else "")
            ),
            {"i": did},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown document")
    if row.lifecycle != "active" or row.scan_status != "clean":
        raise ApiError("document_not_active", "callback ignored: document not active")
    return row


async def cb_quality(uow: UoW, d: Ingest, svc: Principal, doc_id: str, body: Any) -> dict[str, Any]:
    row = await _active_doc(uow, doc_id)
    flags = sorted(set(body.flags))
    await uow.session.execute(
        text(
            "UPDATE document SET quality_score=:q, quality_flags=:f, has_required_stamp=:st WHERE id=:i"
        ),
        {"q": body.quality_score, "f": flags, "st": body.has_required_stamp, "i": row.id},
    )
    changed = (row.quality_score, row.quality_flags, row.has_required_stamp) != (
        body.quality_score,
        flags,
        body.has_required_stamp,
    )
    if changed:
        await audit.append(
            uow.session,
            row.case_id,
            "doc.quality",
            {
                "doc_id": str(row.id),
                "score": body.quality_score,
                "flags": flags,
                "stamp": body.has_required_stamp,
            },
            actor_id=svc.actor_id,
        )
    await uow.commit()
    if changed:
        await d.completeness(str(row.case_id), "doc_event")
    return {"document_id": str(row.id), "quality_score": body.quality_score}


async def _recompute_status(uow: UoW, d: Ingest, row_id: Any) -> dict[str, Any]:
    s = uow.session
    row = (
        await s.execute(
            text("SELECT d.*, d.doc_type::text AS doc_type_t FROM document d WHERE id=:i"),
            {"i": row_id},
        )
    ).one()
    passes = (
        await s.execute(
            text(
                "SELECT pass_no, typed_json, confidence FROM document_parse WHERE document_id=:i "
                "ORDER BY pass_no"
            ),
            {"i": row_id},
        )
    ).all()
    gates = await _gates(uow)
    agree = None
    if len(passes) >= 2:
        agree = parse_logic.agreement(
            passes[0].typed_json or {},
            passes[1].typed_json or {},
            row.doc_type_t,
            d.s.name_match_min,
        )
        await s.execute(
            text(
                "UPDATE document_parse SET agreement_score=:a WHERE document_id=:i AND pass_no=:p"
            ),
            {"a": agree, "i": row_id, "p": passes[1].pass_no},
        )
    conf = passes[-1].confidence if passes else None
    new = parse_logic.derive_status(
        scan_clean=True,
        active=True,
        passes=len(passes),
        doc_type=row.doc_type_t,
        agreement_score=agree,
        parse_confidence=conf,
        gates=gates,
    )
    if new is not None and new != row.parse_status:
        await s.execute(
            text("UPDATE document SET parse_status=:s, parse_confidence=:c WHERE id=:i"),
            {"s": new, "c": conf, "i": row_id},
        )
    elif conf is not None:
        await s.execute(
            text("UPDATE document SET parse_confidence=:c WHERE id=:i"), {"c": conf, "i": row_id}
        )
    return {
        "parse_status": new or row.parse_status,
        "agreement_score": agree,
        "case_id": row.case_id,
    }


async def cb_parse(uow: UoW, d: Ingest, svc: Principal, doc_id: str, body: Any) -> dict[str, Any]:
    row = await _active_doc(uow, doc_id)
    s = uow.session
    payload_hash = hashlib.sha256(
        json.dumps(body.typed_json, sort_keys=True, default=str).encode()
    ).hexdigest()
    prev = (
        await s.execute(
            text("SELECT typed_json FROM document_parse WHERE document_id=:i AND pass_no=:p"),
            {"i": row.id, "p": body.pass_no},
        )
    ).first()
    unchanged = (
        prev is not None
        and hashlib.sha256(
            json.dumps(prev.typed_json, sort_keys=True, default=str).encode()
        ).hexdigest()
        == payload_hash
    )
    await s.execute(
        text(
            "INSERT INTO document_parse (id, document_id, pass_no, engine, engine_version, raw_markdown_key, masked_text_key, "
            "entities, typed_json, confidence, duration_ms) VALUES (uuid_generate_v7(), :d, :p, :e, :ev, :rk, :mk, "
            "CAST(:en AS jsonb), CAST(:tj AS jsonb), :c, :ms) ON CONFLICT ON CONSTRAINT uq_document_parse_pass DO UPDATE "
            "SET engine=EXCLUDED.engine, engine_version=EXCLUDED.engine_version, raw_markdown_key=EXCLUDED.raw_markdown_key, "
            "masked_text_key=EXCLUDED.masked_text_key, entities=EXCLUDED.entities, typed_json=EXCLUDED.typed_json, "
            "confidence=EXCLUDED.confidence, duration_ms=EXCLUDED.duration_ms"
        ),
        {
            "d": row.id,
            "p": body.pass_no,
            "e": body.engine,
            "ev": body.engine_version,
            "rk": body.raw_markdown_key,
            "mk": body.masked_text_key,
            "en": json.dumps(body.entities) if body.entities is not None else None,
            "tj": json.dumps(body.typed_json),
            "c": body.confidence,
            "ms": body.duration_ms,
        },
    )
    if row.parse_status == "pending":
        await s.execute(
            text("UPDATE document SET parse_status='processing' WHERE id=:i"), {"i": row.id}
        )
    res = await _recompute_status(uow, d, row.id)
    if not unchanged:
        await audit.append(
            s,
            row.case_id,
            "doc.parsed",
            {
                "doc_id": str(row.id),
                "pass_no": body.pass_no,
                "engine": body.engine,
                "confidence": body.confidence,
                "payload_sha256": payload_hash,
            },
            actor_id=svc.actor_id,
        )
        if res["agreement_score"] is not None:
            await audit.append(
                s,
                row.case_id,
                "parse.agreement",
                {"doc_id": str(row.id), "score": res["agreement_score"]},
                actor_id=svc.actor_id,
            )

    async def after() -> None:
        await d.hub.publish(
            "document.parsed",
            str(row.case_id),
            {
                "document_id": str(row.id),
                "pass_no": body.pass_no,
                "parse_status": res["parse_status"],
            },
        )

    uow.after_commit(after)
    await uow.commit()
    if res["parse_status"] in ("parsed", "needs_review"):
        await d.completeness(str(row.case_id), "doc_event")
    out = {"document_id": str(row.id), "parse_status": res["parse_status"]}
    if res["agreement_score"] is not None:
        out["agreement_score"] = res["agreement_score"]
    else:
        out["next"] = "pass_2_expected"
    return out


async def cb_classify(
    uow: UoW, d: Ingest, svc: Principal, doc_id: str, body: Any
) -> dict[str, Any]:
    row = await _active_doc(uow, doc_id)
    s = uow.session
    gates = await _gates(uow)
    if row.doc_type_source == "manual":  # manual choice wins forever
        return {
            "document_id": str(row.id),
            "ignored": "manual_classification_wins",
            "doc_type": row.doc_type_t,
        }
    ok = body.doc_type is not None and parse_logic.classification_gate(
        body.confidence, gates["classification_min"]
    )
    if ok:
        await s.execute(
            text(
                "UPDATE document SET doc_type=CAST(:t AS doc_type), doc_type_source='auto', "
                "classification_confidence=:c WHERE id=:i"
            ),
            {"t": body.doc_type.value, "c": body.confidence, "i": row.id},
        )
    else:
        await s.execute(
            text(
                "UPDATE document SET doc_type=NULL, doc_type_source=NULL, classification_confidence=:c, "
                "parse_status='needs_review' WHERE id=:i"
            ),
            {"c": body.confidence, "i": row.id},
        )
    await audit.append(
        s,
        row.case_id,
        "doc.classified",
        {
            "doc_id": str(row.id),
            "doc_type": body.doc_type.value if body.doc_type else None,
            "confidence": body.confidence,
            "accepted": ok,
        },
        actor_id=svc.actor_id,
    )
    res = await _recompute_status(uow, d, row.id) if ok else {"parse_status": "needs_review"}

    async def after() -> None:
        await d.hub.publish(
            "document.classified",
            str(row.case_id),
            {
                "document_id": str(row.id),
                "doc_type": body.doc_type.value if ok and body.doc_type else None,
            },
        )

    uow.after_commit(after)
    await uow.commit()
    await d.completeness(str(row.case_id), "doc_event")
    return {"document_id": str(row.id), "accepted": ok, "parse_status": res["parse_status"]}


async def cb_status(uow: UoW, d: Ingest, svc: Principal, doc_id: str, body: Any) -> dict[str, Any]:
    row = await _active_doc(uow, doc_id)
    await uow.session.execute(
        text("UPDATE document SET parse_status=:s WHERE id=:i"),
        {"s": body.parse_status, "i": row.id},
    )
    await audit.append(
        uow.session,
        row.case_id,
        "doc.parsed",
        {"doc_id": str(row.id), "status": body.parse_status, "error": body.error},
        actor_id=svc.actor_id,
    )

    async def after() -> None:
        await d.hub.publish(
            "document.parsed",
            str(row.case_id),
            {"document_id": str(row.id), "parse_status": body.parse_status},
        )

    uow.after_commit(after)
    await uow.commit()
    return {"document_id": str(row.id), "parse_status": body.parse_status}


async def cb_split(uow: UoW, d: Ingest, svc: Principal, doc_id: str, body: Any) -> dict[str, Any]:
    import pikepdf

    row = await _active_doc(uow, doc_id)
    if row.mime_type != "application/pdf":
        raise ApiError("validation_error", "only PDFs can be split")
    ranges = sorted((c.page_from, c.page_to) for c in body.children)
    for (a, b), (c, _) in zip(ranges, ranges[1:], strict=False):
        if b >= c:
            raise ApiError("validation_error", "page ranges must not overlap")
    for a, b in ranges:
        if a > b or b > row.pages:
            raise ApiError("validation_error", f"page range {a}-{b} outside 1-{row.pages}")
    data = await d.store.get(row.storage_key)
    children: list[str] = []
    s = uow.session
    with pikepdf.open(io.BytesIO(data)) as src:
        for ch in sorted(body.children, key=lambda c: c.page_from):
            out = pikepdf.new()
            for pg in src.pages[ch.page_from - 1 : ch.page_to]:
                out.pages.append(pg)
            buf = io.BytesIO()
            out.save(buf)
            blob = buf.getvalue()
            cid = uuid.uuid4()
            key = f"{row.case_id}/{cid}/original.pdf"
            await d.store.put(
                key, blob, "application/pdf", {"sha256": hashlib.sha256(blob).hexdigest()}
            )
            await s.execute(
                text(
                    "INSERT INTO document (id, case_id, original_filename, mime_type, size_bytes, sha256, storage_key, pages, "
                    "scan_status, doc_type, doc_type_source, parent_id, page_range, uploaded_by, last_trigger_at) VALUES (:id, :c, :n, "
                    "'application/pdf', :sz, :sha, :k, :pg, 'clean', CAST(:dt AS doc_type), :src, :par, "
                    "int4range(:a, :b, '[]'), :u, now())"
                ),
                {
                    "id": cid,
                    "c": row.case_id,
                    "n": f"{row.original_filename} p{ch.page_from}-{ch.page_to}",
                    "sz": len(blob),
                    "sha": hashlib.sha256(blob).hexdigest(),
                    "k": key,
                    "pg": ch.page_to - ch.page_from + 1,
                    "dt": ch.doc_type_hint.value if ch.doc_type_hint else None,
                    "src": "manual" if ch.doc_type_hint else None,
                    "par": row.id,
                    "a": ch.page_from,
                    "b": ch.page_to,
                    "u": row.uploaded_by,
                },
            )
            children.append(str(cid))
    await s.execute(text("UPDATE document SET lifecycle='superseded' WHERE id=:i"), {"i": row.id})
    await audit.append(
        s,
        row.case_id,
        "doc.split",
        {"doc_id": str(row.id), "children": children},
        actor_id=svc.actor_id,
    )

    async def after() -> None:
        for c in children:
            await d.n8n.trigger(
                "intake/document-uploaded", {"case_id": str(row.case_id), "document_id": c}, c
            )
        await d.hub.publish("document.uploaded", str(row.case_id), {"children": children})

    uow.after_commit(after)
    await uow.commit()
    await d.completeness(str(row.case_id), "doc_event")
    return {"parent_id": str(row.id), "children": children}


async def pending_parse(uow: UoW, older_than_s: int, limit: int) -> dict[str, Any]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT id, case_id, parse_attempts FROM document WHERE parse_status='pending' AND scan_status='clean' "
                "AND lifecycle='active' AND (last_trigger_at IS NULL OR last_trigger_at < now() - make_interval(secs => :s)) "
                "ORDER BY created_at LIMIT :l"
            ),
            {"s": older_than_s, "l": limit},
        )
    ).all()
    return {
        "documents": [
            {"id": str(r.id), "case_id": str(r.case_id), "attempts": r.parse_attempts} for r in rows
        ]
    }


# --------------------------------------------------------------------------------------------- jobs
async def sweeper(
    uow: UoW, d: Ingest, older_than_s: int = 120, max_attempts: int = 5
) -> dict[str, int]:
    s = uow.session
    rows = (
        await s.execute(
            text(
                "SELECT id, case_id, parse_attempts FROM document WHERE parse_status='pending' AND scan_status='clean' "
                "AND lifecycle='active' AND (last_trigger_at IS NULL OR last_trigger_at < now() - make_interval(secs => :s)) "
                "ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED"
            ),
            {"s": older_than_s},
        )
    ).all()
    retriggered = failed = 0
    to_trigger: list[Any] = []
    for r in rows:
        if r.parse_attempts + 1 >= max_attempts:
            await s.execute(
                text(
                    "UPDATE document SET parse_status='failed', parse_attempts=parse_attempts+1 WHERE id=:i"
                ),
                {"i": r.id},
            )
            await audit.append(
                s,
                r.case_id,
                "doc.parsed",
                {"doc_id": str(r.id), "status": "failed", "error": "sweeper: max attempts"},
            )
            failed += 1
        else:
            await s.execute(
                text(
                    "UPDATE document SET parse_attempts=parse_attempts+1, last_trigger_at=now() WHERE id=:i"
                ),
                {"i": r.id},
            )
            to_trigger.append(r)
            retriggered += 1

    async def after() -> None:
        for r in to_trigger:
            await d.n8n.trigger(
                "intake/document-uploaded",
                {"case_id": str(r.case_id), "document_id": str(r.id)},
                f"{r.id}:sweep:{r.parse_attempts + 1}",
            )

    uow.after_commit(after)
    await uow.commit()
    return {"retriggered": retriggered, "failed": failed}


async def purge_tombstones(uow: UoW, d: Ingest) -> dict[str, int]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT id, storage_key FROM document WHERE lifecycle='deleted' AND purge_after < now() AND storage_key IS NOT NULL"
            )
        )
    ).all()
    for r in rows:
        await d.store.delete_quietly(r.storage_key)
        await uow.session.execute(
            text("UPDATE document SET storage_key = NULL WHERE id=:i"), {"i": r.id}
        )
    await uow.commit()
    return {"purged": len(rows)}


async def flag_stale_drafts(uow: UoW, days: int = 30) -> dict[str, int]:
    res = await uow.session.execute(
        text(
            "UPDATE claim_case SET stale_flagged_at = now() WHERE status='draft' AND stale_flagged_at IS NULL "
            "AND created_at < now() - make_interval(days => :d) RETURNING id"
        ),
        {"d": days},
    )
    n = len(res.all())
    await uow.commit()
    return {"flagged": n}


async def orphan_scan(uow: UoW, d: Ingest) -> dict[str, Any]:
    keys = set(await d.store.list_keys())
    known = {
        r[0]
        for r in (
            await uow.session.execute(
                text("SELECT storage_key FROM document WHERE storage_key IS NOT NULL")
            )
        )
    }
    orphans = sorted(
        k
        for k in keys - known
        if "/pages/" not in k and not k.endswith(("parsed.md", "masked.txt"))
    )
    return {"orphans": orphans[:200], "count": len(orphans)}


async def read_file(d: Ingest, key: str) -> bytes:
    return await d.store.get(key)
