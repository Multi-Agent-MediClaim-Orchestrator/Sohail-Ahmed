"""Insurer queries: intake, triage, grounded drafts, approval, send (doc 07)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from claim_contract import models as cm
from sqlalchemy import text

from app.auth.principal import Principal
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, callbacks, transitions

# Fallback triage when the crew is down: category -> (action, needs_docs, escalation_risk). Conservative: anything
# touching policy or identity is flagged for two approvers.
FALLBACK = {
    "missing_document": ("send_documents", True, False),
    "illegible_document": ("send_documents", True, False),
    "identity_mismatch": ("clarify", False, True),
    "medical_clarification": ("clarify", False, False),
    "billing_discrepancy": ("clarify", False, False),
    "policy_exclusion": ("clarify", False, True),
    "other": ("clarify", False, False),
}
FORBIDDEN = re.compile(
    r"\b(guarantee[sd]?|will be (?:approved|paid)|legal action|sue|lawsuit|we admit|our fault|"
    r"promise[sd]?)\b",
    re.I,
)
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
MIN_NOTE = 20


def due_for(created: datetime, hours: int = 72) -> datetime:
    return created + timedelta(hours=hours)


async def _load(uow: UoW, query_id: str, *, lock: bool = False) -> Any:
    try:
        qid = uuid.UUID(query_id)
    except ValueError:
        raise ApiError("not_found", "unknown query") from None
    row = (
        await uow.session.execute(
            text(
                "SELECT q.*, q.status::text AS status_t, q.category::text AS cat, c.claim_ref, c.assigned_to AS case_assignee, "
                "c.status::text AS case_status FROM insurer_query q JOIN claim_case c ON c.id=q.case_id WHERE q.id=:i"
                + (" FOR UPDATE OF q" if lock else "")
            ),
            {"i": qid},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown query")
    return row


async def visible(uow: UoW, p: Principal, query_id: str, *, lock: bool = False) -> Any:
    q = await _load(uow, query_id, lock=lock)
    if "admin" in p.roles and not (p.roles & {"officer", "desk"}):
        raise ApiError("forbidden", "administrators cannot read queries")
    if (
        not (p.roles & {"officer"})
        and q.case_assignee is not None
        and str(q.case_assignee) != str(p.id)
    ):
        raise ApiError("not_found", "unknown query")
    return q


# ---- intake -----------------------------------------------------------------------------------------------
async def record_anomaly(uow: UoW, claim_ref: str, detail: dict[str, Any]) -> None:
    row = (
        await uow.session.execute(
            text("SELECT id FROM claim_case WHERE claim_ref=:r"), {"r": claim_ref}
        )
    ).first()
    if row:
        await audit.append(
            uow.session,
            row.id,
            "query.anomaly",
            detail,
            actor_type="external",
            actor_id="insurer",
        )


async def handle_query(
    uow: UoW,
    claim_ref: str,
    seq: int,
    q: cm.Query,
    idem: str,
    raw: dict[str, Any],
    hub: Any,
    n8n: Any,
) -> tuple[int, Any]:
    case = await callbacks.lock_case(uow, claim_ref)
    prev = await callbacks.last_sequence(uow, claim_ref)
    is_new, st, body = await callbacks.record(uow, claim_ref, "queries", seq, idem, raw)
    if not is_new:
        return st, body
    await callbacks.note_gap(uow, case, claim_ref, seq, prev)
    s = uow.session
    existing = (
        await s.execute(
            text(
                "SELECT id, text, revision, status::text AS st FROM insurer_query WHERE insurer_query_id=:i FOR UPDATE"
            ),
            {"i": q.query_id},
        )
    ).first()
    if existing is not None:
        if existing.text != q.text:  # the insurer reworded: bump revision, invalidate approvals
            await s.execute(
                text(
                    "UPDATE insurer_query SET text=:t, revision=revision+1, updated_at=now() WHERE id=:i"
                ),
                {"t": q.text, "i": existing.id},
            )
            await s.execute(
                text(
                    "UPDATE query_response SET status='superseded' WHERE query_id=:i AND status IN ('draft','needs_attention','approved')"
                ),
                {"i": existing.id},
            )
            await audit.append(
                s,
                case.id,
                "query.received",
                {"query_id": str(q.query_id), "revision": existing.revision + 1, "reworded": True},
                actor_type="external",
                actor_id="insurer",
            )
        return 204, None
    # a later round supersedes still-open earlier rounds
    await s.execute(
        text(
            "UPDATE insurer_query SET status='closed', closed_reason='superseded_by_round', updated_at=now() "
            "WHERE case_id=:c AND round < :r AND status IN ('open','draft_ready')"
        ),
        {"c": case.id, "r": q.round},
    )
    action, needs_docs, risk = FALLBACK[q.category.value]
    qid = (
        await s.execute(
            text(
                "INSERT INTO insurer_query (id, case_id, insurer_query_id, round, category, text, requested_doc_types, status, due_by, "
                "triage, triage_source, escalation_risk) VALUES (uuid_generate_v7(), :c, :iq, :r, CAST(:cat AS query_category), :t, "
                "CAST(:docs AS doc_type[]), 'open', :due, CAST(:tr AS jsonb), 'rules', :risk) RETURNING id"
            ),
            {
                "c": case.id,
                "iq": q.query_id,
                "r": q.round,
                "cat": q.category.value,
                "t": q.text,
                "docs": [d.value for d in q.requested_doc_types],
                "due": q.due_by,
                "tr": json.dumps({"action": action, "needs_docs": needs_docs}),
                "risk": risk or q.round >= 3,
            },
        )
    ).scalar()
    await audit.append(
        s,
        case.id,
        "query.received",
        {"query_id": str(q.query_id), "round": q.round, "category": q.category.value},
        actor_type="external",
        actor_id="insurer",
    )
    await _open_doc_requests(uow, case, q)
    await callbacks.apply_if_valid(uow, case, "under_query", hub, "insurer raised a query")

    async def after() -> None:
        await hub.publish("query.new", str(case.id), {"query_id": str(qid), "round": q.round})
        await n8n.trigger(
            "query/intake",
            {"query_id": str(qid), "case_id": str(case.id), "round": q.round},
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"query-intake:{q.query_id}")),
        )

    uow.after_commit(after)
    return 204, None


async def _open_doc_requests(uow: UoW, case: Any, q: cm.Query) -> None:
    s = uow.session
    for dt in q.requested_doc_types:
        have = (
            await s.execute(
                text(
                    "SELECT 1 FROM document WHERE case_id=:c AND doc_type=CAST(:d AS doc_type) AND lifecycle='active' LIMIT 1"
                ),
                {"c": case.id, "d": dt.value},
            )
        ).first()
        if have:
            continue
        await s.execute(
            text(
                "INSERT INTO doc_request (id, case_id, doc_type, rule_id, reason, reason_code, status, due_by, opened_by_run, last_seen_run) "
                "VALUES (uuid_generate_v7(), :c, CAST(:d AS doc_type), 'insurer_query', :m, 'insurer_requested', 'open', :due, 0, 0) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "c": case.id,
                "d": dt.value,
                "m": f"The insurer asked for: {dt.value.replace('_', ' ')}",
                "due": q.due_by,
            },
        )


# ---- triage / draft ---------------------------------------------------------------------------------------
async def set_triage(
    uow: UoW, query_id: str, triage: dict[str, Any], source: str, actor: Principal | None, hub: Any
) -> dict[str, Any]:
    q = await _load(uow, query_id, lock=True)
    risk = bool(triage.get("escalation_risk", q.escalation_risk)) or q.round >= 3
    await uow.session.execute(
        text(
            "UPDATE insurer_query SET triage=CAST(:t AS jsonb), triage_source=:s, escalation_risk=:r, updated_at=now() WHERE id=:i"
        ),
        {"t": json.dumps(triage), "s": source, "r": risk, "i": q.id},
    )
    await audit.append(
        uow.session,
        q.case_id,
        "query.triaged",
        {"query_id": str(q.id), "source": source, "escalation_risk": risk},
        actor_type="human" if actor else "system",
        actor_id=actor.actor_id if actor else "crew",
    )
    await uow.commit()
    await hub.publish("query.updated", str(q.case_id), {"query_id": str(q.id)})
    return {"query_id": str(q.id), "triage_source": source, "escalation_risk": risk}


def normalise_num(tok: str) -> str:
    t = tok.replace(",", "")
    return t.rstrip("0").rstrip(".") if "." in t else t


def grounding_check(
    draft: str, citations: list[dict[str, Any]], evidence: str
) -> list[dict[str, str]]:
    """Reject-by-flag guard: returns the unsupported claims (empty = grounded).
    G01 empty/short, G02 forbidden commitment, G03 number not in evidence, G04 citation without quote,
    G05 citation quote not in evidence, G06 no citations at all for a factual draft."""
    out: list[dict[str, str]] = []
    if len(draft.strip()) < 20:
        out.append({"rule": "G01", "detail": "draft is too short"})
    for m in FORBIDDEN.finditer(draft):
        out.append({"rule": "G02", "detail": f"forbidden commitment: {m.group(0)}"})
    ev_nums = {normalise_num(n) for n in NUM.findall(evidence)}
    for n in NUM.findall(draft):
        if len(n.replace(",", "")) >= 2 and normalise_num(n) not in ev_nums:
            out.append({"rule": "G03", "detail": f"number not found in the record: {n}"})
    low = evidence.lower()
    for c in citations:
        quote = (c.get("quote") or "").strip()
        if not quote:
            out.append({"rule": "G04", "detail": f"citation {c.get('source_id')} has no quote"})
        elif quote.lower() not in low:
            out.append({"rule": "G05", "detail": f"quote not in the record: {quote[:40]}"})
    if not citations and NUM.search(draft):
        out.append({"rule": "G06", "detail": "numbers cited without any source"})
    return out


async def _evidence(uow: UoW, q: Any) -> str:
    s = uow.session
    parts = [q.text]
    d = (
        await s.execute(
            text("SELECT payload FROM claim_draft WHERE case_id=:c ORDER BY version DESC LIMIT 1"),
            {"c": q.case_id},
        )
    ).first()
    if d:
        parts.append(json.dumps(d.payload))
    for r in (
        await s.execute(
            text(
                "SELECT original_filename, doc_type::text AS dtype FROM document WHERE case_id=:c AND lifecycle='active'"
            ),
            {"c": q.case_id},
        )
    ).all():
        parts += [r.original_filename, r.dtype]
    return "\n".join(parts)


async def draft_result(uow: UoW, query_id: str, body: dict[str, Any], hub: Any) -> dict[str, Any]:
    q = await _load(uow, query_id, lock=True)
    s = uow.session
    if q.status_t in ("answered", "closed"):
        raise ApiError("query_closed", "the query is no longer open", status=409)
    text_ = str(body.get("draft_text", ""))
    cites = list(body.get("citations") or [])
    unsupported = grounding_check(text_, cites, await _evidence(uow, q))
    status = "needs_attention" if unsupported else "draft"
    ver = int(
        (
            await s.execute(
                text("SELECT COALESCE(max(version),0)+1 FROM query_response WHERE query_id=:i"),
                {"i": q.id},
            )
        ).scalar()
    )
    await s.execute(
        text(
            "UPDATE query_response SET status='superseded' WHERE query_id=:i AND status IN ('draft','needs_attention')"
        ),
        {"i": q.id},
    )
    await s.execute(
        text(
            "INSERT INTO query_response (id, query_id, version, draft_text, attached_doc_ids, source, status, created_by, citations, "
            "unsupported_claims, model_info) VALUES (uuid_generate_v7(), :q, :v, :t, CAST(:a AS uuid[]), 'agent', :st, 'crew', "
            "CAST(:c AS jsonb), CAST(:u AS jsonb), CAST(:m AS jsonb))"
        ),
        {
            "q": q.id,
            "v": ver,
            "t": text_,
            "a": [str(x) for x in body.get("attached_doc_ids", [])],
            "st": status,
            "c": json.dumps(cites),
            "u": json.dumps(unsupported),
            "m": json.dumps(body.get("model_info") or {}),
        },
    )
    await s.execute(
        text(
            "UPDATE insurer_query SET status='draft_ready', updated_at=now() WHERE id=:i AND status='open'"
        ),
        {"i": q.id},
    )
    await audit.append(
        s,
        q.case_id,
        "query.draft_generated",
        {"query_id": str(q.id), "version": ver, "status": status, "unsupported": len(unsupported)},
        actor_type="system",
        actor_id="crew",
        model_info=body.get("model_info"),
    )
    await uow.commit()
    await hub.publish(
        "query.draft_ready", str(q.case_id), {"query_id": str(q.id), "status": status}
    )
    return {"version": ver, "status": status, "unsupported_claims": unsupported}


async def _latest(uow: UoW, qid: Any, *, lock: bool = False) -> Any:
    return (
        await uow.session.execute(
            text(
                "SELECT * FROM query_response WHERE query_id=:i AND status <> 'superseded' ORDER BY version DESC LIMIT 1"
                + (" FOR UPDATE" if lock else "")
            ),
            {"i": qid},
        )
    ).first()


async def edit(
    uow: UoW,
    p: Principal,
    query_id: str,
    draft_text: str,
    attached: list[str],
    hub: Any,
    expected_version: int | None,
) -> dict[str, Any]:
    q = await visible(uow, p, query_id, lock=True)
    if q.status_t in ("answered", "closed"):
        raise ApiError("query_closed", "the query is no longer open", status=409)
    cur = await _latest(uow, q.id, lock=True)
    if expected_version is not None and cur and cur.version != expected_version:
        raise ApiError("version_conflict", "the draft changed; reload it", status=409)
    s = uow.session
    ev = await _evidence(uow, q)
    cites = (cur.citations or []) if cur else []
    unsupported = grounding_check(draft_text, cites, ev)
    ver = (cur.version if cur else 0) + 1
    if cur:
        await s.execute(
            text("UPDATE query_response SET status='superseded' WHERE id=:i"), {"i": cur.id}
        )
    await s.execute(
        text(
            "INSERT INTO query_response (id, query_id, version, draft_text, attached_doc_ids, source, status, created_by, citations, "
            "unsupported_claims) VALUES (uuid_generate_v7(), :q, :v, :t, CAST(:a AS uuid[]), 'human_edit', :st, :by, CAST(:c AS jsonb), "
            "CAST(:u AS jsonb))"
        ),
        {
            "q": q.id,
            "v": ver,
            "t": draft_text,
            "a": attached,
            "st": "needs_attention" if unsupported else "draft",
            "by": p.actor_id,
            "c": json.dumps(cites),
            "u": json.dumps(unsupported),
        },
    )
    await s.execute(
        text(
            "UPDATE insurer_query SET status='draft_ready', updated_at=now() WHERE id=:i AND status='open'"
        ),
        {"i": q.id},
    )
    await audit.append(
        s,
        q.case_id,
        "query.edited",
        {"query_id": str(q.id), "version": ver},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    await hub.publish("query.updated", str(q.case_id), {"query_id": str(q.id)})
    return {"version": ver, "unsupported_claims": unsupported}


def approvals_needed(q: Any) -> int:
    return (
        2
        if (q.round >= 3 or q.escalation_risk or q.cat in ("policy_exclusion", "identity_mismatch"))
        else 1
    )


async def approve(
    uow: UoW, p: Principal, query_id: str, override_note: str | None, hub: Any
) -> dict[str, Any]:
    if "officer" not in p.roles:
        raise ApiError("forbidden", "only officers approve query responses")
    q = await visible(uow, p, query_id, lock=True)
    if q.status_t in ("answered", "closed"):
        raise ApiError("query_closed", "the query is no longer open", status=409)
    cur = await _latest(uow, q.id, lock=True)
    if cur is None:
        raise ApiError("no_draft", "there is no draft to approve", status=409)
    s = uow.session
    if cur.status == "sent":
        raise ApiError("already_sent", "this response was already sent", status=409)
    if cur.status == "needs_attention" and not cur.override_note:
        note = (override_note or "").strip()
        if len(note) < MIN_NOTE:
            raise ApiError(
                "override_note_required",
                f"a note of at least {MIN_NOTE} characters is required to approve a flagged draft",
                status=422,
            )
        await s.execute(
            text("UPDATE query_response SET override_note=:n WHERE id=:i"), {"n": note, "i": cur.id}
        )
    me = p.id
    if cur.approved_by is not None and str(cur.approved_by) == str(me):
        raise ApiError("duplicate_approver", "you already approved this draft", status=409)
    need = approvals_needed(q)
    if cur.approved_by is None:
        await s.execute(
            text("UPDATE query_response SET approved_by=:u, approved_at=now() WHERE id=:i"),
            {"u": me, "i": cur.id},
        )
        done = need == 1
    else:
        await s.execute(
            text("UPDATE query_response SET second_approver=:u WHERE id=:i"), {"u": me, "i": cur.id}
        )
        done = True
    if done:
        await s.execute(
            text("UPDATE query_response SET status='approved' WHERE id=:i"), {"i": cur.id}
        )
    await audit.append(
        s,
        q.case_id,
        "query.approved",
        {"query_id": str(q.id), "version": cur.version, "needed": need, "complete": done},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    await hub.publish("query.updated", str(q.case_id), {"query_id": str(q.id)})
    return {"approved": done, "approvals_needed": need, "version": cur.version}


async def send(uow: UoW, p: Principal, query_id: str, hub: Any, store: Any) -> dict[str, Any]:
    if "officer" not in p.roles:
        raise ApiError("forbidden", "only officers send query responses")
    q = await visible(uow, p, query_id, lock=True)
    cur = await _latest(uow, q.id, lock=True)
    if cur is None or cur.status != "approved":
        raise ApiError(
            "not_approved", "the response needs the required approvals first", status=409
        )
    s = uow.session
    case = (
        await s.execute(text("SELECT * FROM claim_case WHERE id=:i FOR UPDATE"), {"i": q.case_id})
    ).one()
    settings_ttl = 3600 * 24 * 3
    from app.services.submission import doc_refs_for

    attached = [str(x) for x in (cur.attached_doc_ids or [])]
    seq = int(
        (
            await s.execute(
                text("SELECT COALESCE(max(sequence),0)+1 FROM outbox WHERE case_id=:c"),
                {"c": case.id},
            )
        ).scalar()
    )
    if attached:
        refs = await doc_refs_for(uow, store, case, attached, settings_ttl)
        body = {
            "reason": "query_response",
            "query_id": str(q.insurer_query_id),
            "documents": refs,
        }
        await _enqueue(
            uow,
            case,
            "claim.documents",
            "POST",
            f"/v1/hospital-api/claims/{case.claim_ref}/documents",
            body,
            f"{case.claim_ref}:{q.insurer_query_id}:docs:{cur.version}",
            seq,
        )
        seq += 1
    body = {
        "query_id": str(q.insurer_query_id),
        "answer_text": cur.draft_text,
        "attached_doc_ids": attached,
        "responded_by": p.email or p.actor_id,
        "responded_at": datetime.now(UTC).isoformat(),
    }
    await _enqueue(
        uow,
        case,
        "query.response",
        "POST",
        f"/v1/hospital-api/queries/{q.insurer_query_id}/responses",
        body,
        f"{case.claim_ref}:{q.insurer_query_id}:resp:{cur.version}",
        seq,
    )
    await s.execute(
        text("UPDATE query_response SET status='sent', sent_at=now() WHERE id=:i"), {"i": cur.id}
    )
    await s.execute(
        text(
            "UPDATE insurer_query SET status='answered', responded_at=now(), updated_at=now() WHERE id=:i"
        ),
        {"i": q.id},
    )
    await audit.append(
        s,
        q.case_id,
        "query.sent",
        {"query_id": str(q.id), "version": cur.version, "docs": len(attached)},
        actor_type="human",
        actor_id=p.actor_id,
    )
    open_left = (
        await s.execute(
            text(
                "SELECT count(*) FROM insurer_query WHERE case_id=:c AND status IN ('open','draft_ready','escalated')"
            ),
            {"c": q.case_id},
        )
    ).scalar()
    if not open_left:
        cur_status = (
            await s.execute(
                text("SELECT status::text FROM claim_case WHERE id=:i"), {"i": q.case_id}
            )
        ).scalar()
        if cur_status == "under_query":
            await transitions.transition(
                uow, q.case_id, "acknowledged", p, reason="all queries answered", hub=hub
            )
    await uow.commit()
    await hub.publish("query.updated", str(q.case_id), {"query_id": str(q.id)})
    return {"status": "answered", "outbox_rows": 2 if attached else 1}


async def _enqueue(
    uow: UoW,
    case: Any,
    kind: str,
    method: str,
    path: str,
    body: dict[str, Any],
    idem_basis: str,
    seq: int,
) -> None:
    from claim_contract.outbox import canonical_bytes

    raw = canonical_bytes(body)
    await uow.session.execute(
        text(
            "INSERT INTO outbox (id, case_id, kind, method, path, body, body_sha256, idempotency_key, sequence, status) VALUES "
            "(uuid_generate_v7(), :c, :k, :m, :p, CAST(:b AS jsonb), :h, :i, :n, 'pending')"
        ),
        {
            "c": case.id,
            "k": kind,
            "m": method,
            "p": path,
            "b": json.dumps(body),
            "h": hashlib.sha256(raw).hexdigest(),
            "i": uuid.uuid5(uuid.NAMESPACE_URL, idem_basis),
            "n": seq,
        },
    )


# ---- listing ------------------------------------------------------------------------------------------------
async def inbox(
    uow: UoW, p: Principal, status: str | None, limit: int, cursor: str | None
) -> dict[str, Any]:
    if "admin" in p.roles and not (p.roles & {"officer", "desk"}):
        return {"items": [], "next_cursor": None, "limit": limit, "counts": {}}
    from app.auth.deps import case_scope_sql

    sql, params = case_scope_sql(p)
    s = uow.session
    where = [sql]
    if status:
        where.append("q.status = CAST(:st AS query_status)")
        params["st"] = status
    if cursor:
        due, _, i = cursor.partition("|")
        where.append("(q.due_by, q.id) > (CAST(:cd AS timestamptz), CAST(:ci AS uuid))")
        params.update(cd=due, ci=i)
    rows = (
        await s.execute(
            text(
                "SELECT q.id, q.case_id, q.round, q.category::text AS category, q.status::text AS status, q.due_by, q.escalation_risk, "
                "q.triage_source, c.claim_ref, "
                "(SELECT string_agg(left(w, 1), '') FROM unnest(string_to_array(p.full_name, ' ')) w) AS initials "
                "FROM insurer_query q JOIN claim_case c ON c.id=q.case_id JOIN patient p ON p.id=c.patient_id WHERE "
                + " AND ".join(where)
                + " ORDER BY q.due_by NULLS LAST, q.id LIMIT :lim"
            ),
            {**params, "lim": limit + 1},
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "case_id": str(r.case_id),
            "claim_ref": r.claim_ref,
            "round": r.round,
            "category": r.category,
            "status": r.status,
            "due_by": r.due_by.isoformat() if r.due_by else None,
            "escalation_risk": r.escalation_risk,
            "patient_initials": r.initials,
            "overdue": bool(
                r.due_by and r.due_by < datetime.now(UTC) and r.status in ("open", "draft_ready")
            ),
        }
        for r in rows[:limit]
    ]
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = f"{last.due_by.isoformat() if last.due_by else '9999-12-31T00:00:00+00:00'}|{last.id}"
    sql2, p2 = case_scope_sql(p)
    counts = {
        r.status: r.n
        for r in (
            await s.execute(
                text(
                    "SELECT q.status::text AS status, count(*) AS n FROM insurer_query q JOIN claim_case c ON c.id=q.case_id WHERE "
                    + sql2
                    + " GROUP BY 1"
                ),
                p2,
            )
        ).all()
    }
    return {"items": items, "next_cursor": nxt, "limit": limit, "counts": counts}


async def detail(uow: UoW, p: Principal, query_id: str) -> dict[str, Any]:
    q = await visible(uow, p, query_id)
    s = uow.session
    resps = (
        await s.execute(
            text("SELECT * FROM query_response WHERE query_id=:i ORDER BY version"), {"i": q.id}
        )
    ).all()
    return {
        "id": str(q.id),
        "case_id": str(q.case_id),
        "claim_ref": q.claim_ref,
        "round": q.round,
        "category": q.cat,
        "text": q.text,
        "status": q.status_t,
        "due_by": q.due_by.isoformat() if q.due_by else None,
        "requested_doc_types": [
            str(x.value if hasattr(x, "value") else x) for x in q.requested_doc_types
        ],
        "triage": q.triage,
        "triage_source": q.triage_source,
        "escalation_risk": q.escalation_risk,
        "revision": q.revision,
        "approvals_needed": approvals_needed(q),
        "responses": [
            {
                "version": r.version,
                "status": r.status,
                "source": r.source,
                "draft_text": r.draft_text,
                "attached_doc_ids": [str(x) for x in r.attached_doc_ids or []],
                "citations": r.citations,
                "unsupported_claims": r.unsupported_claims,
                "approved_by": str(r.approved_by) if r.approved_by else None,
                "second_approver": str(r.second_approver) if r.second_approver else None,
                "override_note": r.override_note,
            }
            for r in resps
        ],
    }


# ---- overdue (100% only; no 50/80% reminders by decision) ------------------------------------------------------
async def overdue_job(uow: UoW, hub: Any) -> dict[str, int]:
    s = uow.session
    rows = (
        await s.execute(
            text(
                "UPDATE insurer_query SET overdue_notified_at=now() WHERE status IN ('open','draft_ready') AND due_by < now() AND "
                "overdue_notified_at IS NULL RETURNING id, case_id"
            )
        )
    ).all()
    for r in rows:
        await audit.append(s, r.case_id, "query.overdue", {"query_id": str(r.id)})
    await uow.commit()
    for r in rows:
        await hub.publish("query.overdue", str(r.case_id), {"query_id": str(r.id)})
    return {"notified": len(rows)}


# ---- outbox success handlers: remember what the insurer has received --------------------------------------------
async def _mark_sent(uow: UoW, row: Any, body: Any, app: Any) -> None:
    doc_ids = [d["doc_id"] for d in (row["body"] or {}).get("documents", [])]
    if doc_ids:
        await uow.session.execute(
            text("UPDATE document SET sent_at=now() WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": doc_ids},
        )


def _register() -> None:
    from app.outbox.worker import on_success

    on_success("claim.documents")(_mark_sent)


_register()
