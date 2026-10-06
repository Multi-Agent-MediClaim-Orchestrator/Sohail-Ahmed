"""Case lifecycle: create, read, list (keyset), patch (optimistic lock), assign, timeline."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from app.auth.deps import case_scope_sql
from app.auth.principal import Principal
from app.core import crypto
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.uow import UoW
from app.schemas.cases import CaseCreate, CasePatch
from app.services import audit, config_service, transitions

EDITABLE_STATUSES = {"draft", "docs_pending", "docs_complete"}
ROUTE_FIELDS = {
    "admitted_on",
    "discharged_on",
    "admitted_at",
    "discharged_at",
    "admission_source",
    "diagnosis_codes",
    "procedure_codes",
    "preauth_ref",
    "policy",
}


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def encode_cursor(ts: datetime, id_: Any) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"t": ts.isoformat(), "i": str(id_)}).encode()
    ).decode()


def decode_cursor(c: str) -> tuple[datetime, uuid.UUID]:
    try:
        d = json.loads(base64.urlsafe_b64decode(c.encode()))
        return datetime.fromisoformat(d["t"]), uuid.UUID(d["i"])
    except (ValueError, KeyError, json.JSONDecodeError):
        raise ApiError("bad_request", "invalid cursor") from None


async def _hospital_id(uow: UoW) -> uuid.UUID:
    r = (
        await uow.session.execute(text("SELECT id FROM hospital ORDER BY created_at LIMIT 1"))
    ).scalar()
    if r is None:
        raise ApiError("internal_error", "no hospital configured")
    return r  # type: ignore[no-any-return]


async def create_case(
    uow: UoW,
    p: Principal,
    req: CaseCreate,
    settings: Settings,
    hub: Any,
    confirm_patient_update: bool = False,
) -> dict[str, Any]:
    s = uow.session
    pat = (
        await s.execute(
            text("SELECT * FROM patient WHERE uhid = :u FOR UPDATE"), {"u": req.patient.uhid}
        )
    ).first()
    phone_enc = (
        crypto.encrypt(req.patient.phone, settings.field_key_b64) if req.patient.phone else None
    )
    if pat is not None:
        differs = (
            pat.full_name.strip().lower() != req.patient.full_name.lower()
            or pat.dob != req.patient.dob
        )
        if differs and not confirm_patient_update:
            raise ApiError(
                "patient_conflict",
                "a patient with this UHID exists with different details",
                stored={"full_name": pat.full_name, "dob": pat.dob.isoformat()},
            )
        await s.execute(
            text(
                "UPDATE patient SET full_name=:n, dob=:d, gender=:g, "
                "phone_enc=COALESCE(:ph, phone_enc) WHERE id=:i"
            ),
            {
                "n": req.patient.full_name,
                "d": req.patient.dob,
                "g": req.patient.gender,
                "ph": phone_enc,
                "i": pat.id,
            },
        )
        patient_id = pat.id
    else:
        patient_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO patient (id, uhid, full_name, dob, gender, phone_enc) "
                "VALUES (uuid_generate_v7(), :u, :n, :d, :g, :ph)"
            ),
            {
                "u": req.patient.uhid,
                "n": req.patient.full_name,
                "d": req.patient.dob,
                "g": req.patient.gender,
                "ph": phone_enc,
            },
        )
        patient_id = (
            await s.execute(text("SELECT id FROM patient WHERE uhid=:u"), {"u": req.patient.uhid})
        ).scalar()
    policy_id = (
        await s.execute(
            text(
                "INSERT INTO insurance_policy_ref (id, patient_id, insurer_name, policy_number, member_id) "
                "VALUES (uuid_generate_v7(), :p, :i, :n, :m) ON CONFLICT ON CONSTRAINT uq_policy_ref "
                "DO UPDATE SET insurer_name = EXCLUDED.insurer_name RETURNING id"
            ),
            {
                "p": patient_id,
                "i": req.policy.insurer_name,
                "n": req.policy.policy_number,
                "m": req.policy.member_id,
            },
        )
    ).scalar()
    cfg = await config_service.snapshot(s)
    deadlines = (await config_service.resolve(s, "deadlines"))[1]
    filing = None
    if req.claim_type.value == "reimbursement" and req.discharged_on:
        filing = req.discharged_on + timedelta(days=deadlines["reimbursement_filing_days"])
    ref = (await s.execute(text("SELECT next_claim_ref()"))).scalar()
    case_id = (
        await s.execute(
            text(
                "INSERT INTO claim_case (id, claim_ref, hospital_id, patient_id, policy_ref_id, claim_type, "
                "admission_type, admitted_on, discharged_on, admitted_at, discharged_at, admission_source, diagnosis_codes, "
                "procedure_codes, treating_doctor, preauth_ref, filing_deadline, config_versions, created_by, route) "
                "VALUES (uuid_generate_v7(), :ref, :h, :p, :pol, CAST(:ct AS claim_type), CAST(:at AS admission_type), "
                ":ad, :dd, :ada, :dda, :src, :dx, :px, :doc, :pre, :fd, CAST(:cv AS jsonb), :u, CAST(:route AS jsonb)) "
                "RETURNING id"
            ),
            {
                "ref": ref,
                "h": await _hospital_id(uow),
                "p": patient_id,
                "pol": policy_id,
                "ct": req.claim_type.value,
                "at": req.admission_type.value,
                "ad": req.admitted_on,
                "dd": req.discharged_on,
                "ada": req.admitted_at,
                "dda": req.discharged_at,
                "src": req.admission_source
                or ("ER" if req.admission_type.value == "emergency" else "OPD"),  # form default
                "route": json.dumps(
                    {
                        "proposal": {
                            "claim_type": req.claim_type.value,
                            "admission_type": req.admission_type.value,
                            "by": str(p.id),
                        }
                    }
                ),
                "dx": req.diagnosis_codes,
                "px": req.procedure_codes,
                "doc": req.treating_doctor,
                "pre": req.preauth_ref,
                "fd": filing,
                "cv": json.dumps(cfg),
                "u": p.id,
            },
        )
    ).scalar()
    await s.execute(
        text(
            "INSERT INTO case_status_history (id, case_id, from_status, to_status, actor_id) "
            "VALUES (uuid_generate_v7(), :c, NULL, 'draft', :a)"
        ),
        {"c": case_id, "a": p.actor_id},
    )
    await audit.append(
        s,
        case_id,
        "case.created",
        {"claim_type": req.claim_type.value, "admission_type": req.admission_type.value},
        actor_type="human",
        actor_id=p.actor_id,
        config_versions=cfg,
    )
    from app.router_engine import service as router_svc  # noqa: PLC0415  (avoids an import cycle)

    await router_svc.recompute(uow, case_id, "create", p.actor_id, hub=hub, commit=False)
    route = await router_svc.get_route(uow, case_id)
    warnings = route["warnings"]
    filing = (
        await s.execute(text("SELECT filing_deadline FROM claim_case WHERE id=:i"), {"i": case_id})
    ).scalar()

    async def publish() -> None:
        await hub.publish("case.created", str(case_id), {"claim_ref": ref})

    uow.after_commit(publish)
    await uow.commit()
    return {
        "id": str(case_id),
        "claim_ref": ref,
        "status": "draft",
        "version": 1,
        "filing_deadline": _iso(filing),
        "claim_type": route["decision"]["pipeline"],
        "route": {
            "pipeline": route["decision"]["pipeline"],
            "admission_type": route["decision"]["admission_type"],
            "flags": route["decision"]["flags"],
            "provisional": route["provisional"],
        },
        "warnings": warnings,
    }


LIST_SQL = """
SELECT c.id, c.claim_ref, c.claim_type::text AS claim_type, c.status::text AS status, c.flags, c.claimed_amount,
       c.assigned_to, c.created_at, c.updated_at, p.full_name AS patient_name, u.display_name AS assignee_name,
       (SELECT count(*) FROM document d WHERE d.case_id = c.id AND d.lifecycle = 'active') AS docs_total,
       (SELECT count(*) FROM document d WHERE d.case_id = c.id AND d.lifecycle = 'active'
          AND (d.parse_status IN ('needs_review', 'failed') OR d.doc_type IS NULL)) AS docs_attention
FROM claim_case c JOIN patient p ON p.id = c.patient_id LEFT JOIN app_user u ON u.id = c.assigned_to
WHERE {where}
ORDER BY c.created_at DESC, c.id DESC LIMIT :limit
"""


async def list_cases(
    uow: UoW,
    p: Principal,
    *,
    statuses: list[str] | None,
    q: str | None,
    assigned: str | None,
    claim_type: str | None,
    flag: str | None,
    cursor: str | None,
    size: int,
) -> dict[str, Any]:
    scope_sql, params = case_scope_sql(p)
    where = [scope_sql]
    if statuses:
        where.append("c.status = ANY(CAST(:statuses AS hospital_case_status[]))")
        params["statuses"] = statuses
    if q:
        if len(q) < 3:
            raise ApiError("validation_error", "q must be at least 3 characters")
        where.append("(p.full_name % :q OR c.claim_ref ILIKE :qp OR p.uhid ILIKE :qp)")
        params.update({"q": q, "qp": q + "%"})
    if assigned == "me":
        where.append("c.assigned_to = :me")
        params["me"] = p.id
    elif assigned == "unassigned":
        where.append("c.assigned_to IS NULL")
    elif assigned:
        try:
            params["asg"] = uuid.UUID(assigned)
        except ValueError:
            raise ApiError(
                "validation_error", "assigned must be me, unassigned or a uuid"
            ) from None
        where.append("c.assigned_to = :asg")
    if claim_type:
        where.append("c.claim_type = CAST(:ct AS claim_type)")
        params["ct"] = claim_type
    if flag:
        where.append(":flag = ANY(c.flags)")
        params["flag"] = flag
    if cursor:
        ts, cid = decode_cursor(cursor)
        where.append("(c.created_at, c.id) < (:cts, :cid)")
        params.update({"cts": ts, "cid": cid})
    params["limit"] = size + 1
    s = uow.session
    if q:
        await s.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.3"))
    sql = LIST_SQL.format(where=" AND ".join(where))
    rows = (await s.execute(text(sql), params)).all()
    page = rows[:size]
    count_params = {k: v for k, v in params.items() if k not in ("limit", "cts", "cid")}
    count_where = [w for w in where if "cts" not in w]
    count_sql = (
        "SELECT count(*) FROM (SELECT 1 FROM claim_case c JOIN patient p ON p.id = c.patient_id WHERE "
        + " AND ".join(count_where)
        + " LIMIT 10001) t"
    )
    total = (await s.execute(text(count_sql), count_params)).scalar()
    items = [
        {
            "id": str(r.id),
            "claim_ref": r.claim_ref,
            "patient_name": r.patient_name,
            "claim_type": r.claim_type,
            "status": r.status,
            "flags": r.flags,
            "assigned_to": {"id": str(r.assigned_to), "name": r.assignee_name}
            if r.assigned_to
            else None,
            "claimed_amount": str(r.claimed_amount) if r.claimed_amount is not None else None,
            "doc_counts": {"total": r.docs_total, "needs_attention": r.docs_attention},
            "updated_at": _iso(r.updated_at),
        }
        for r in page
    ]
    nxt = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > size and page else None
    return {"items": items, "next_cursor": nxt, "total_estimate": total}


async def load_case(uow: UoW, p: Principal, case_id: str, *, for_update: bool = False) -> Any:
    """Fetch a case row if visible to the principal, else 404 (no existence leak)."""
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise ApiError("not_found", "unknown case") from None
    scope_sql, params = case_scope_sql(p)
    lock = " FOR UPDATE OF c" if for_update else ""
    row = (
        await uow.session.execute(
            text(
                f"SELECT c.*, c.status::text AS status_t, c.claim_type::text AS claim_type_t, "  # noqa: S608
                f"c.admission_type::text AS admission_type_t FROM claim_case c WHERE c.id = :i AND {scope_sql}{lock}"
            ),
            {"i": cid, **params},
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown case")
    return row


async def get_case(uow: UoW, p: Principal, case_id: str, settings: Settings) -> dict[str, Any]:
    c = await load_case(uow, p, case_id)
    s = uow.session
    pat = (await s.execute(text("SELECT * FROM patient WHERE id = :i"), {"i": c.patient_id})).one()
    pol = (
        await s.execute(
            text("SELECT * FROM insurance_policy_ref WHERE id = :i"), {"i": c.policy_ref_id}
        )
    ).one()
    comp = (
        await s.execute(
            text(
                "SELECT run_no, complete, result FROM completeness_check WHERE case_id=:c "
                "ORDER BY run_no DESC LIMIT 1"
            ),
            {"c": c.id},
        )
    ).first()
    completeness = None
    if comp:
        items = comp.result.get("items", [])
        completeness = {
            "run_no": comp.run_no,
            "complete": comp.complete,
            "blockers": sum(1 for i in items if i.get("severity") == "blocker"),
            "warnings": sum(1 for i in items if i.get("severity") == "warning"),
        }
    last4 = None
    if pat.phone_enc is not None and settings.field_key_b64:
        last4 = crypto.decrypt(bytes(pat.phone_enc), settings.field_key_b64)[-4:]
    assignee = None
    if c.assigned_to:
        assignee = (
            await s.execute(
                text("SELECT id, display_name FROM app_user WHERE id=:i"), {"i": c.assigned_to}
            )
        ).first()
    return {
        "id": str(c.id),
        "claim_ref": c.claim_ref,
        "status": c.status_t,
        "version": c.version,
        "patient": {
            "id": str(pat.id),
            "uhid": pat.uhid,
            "full_name": pat.full_name,
            "dob": _iso(pat.dob),
            "gender": pat.gender,
            "phone_last4": last4,
        },
        "policy": {
            "insurer_name": pol.insurer_name,
            "policy_number": pol.policy_number,
            "member_id": pol.member_id,
        },
        "claim_type": c.claim_type_t,
        "admission_type": c.admission_type_t,
        "route": c.route,
        "flags": c.flags,
        "admitted_on": _iso(c.admitted_on),
        "discharged_on": _iso(c.discharged_on),
        "preauth_ref": c.preauth_ref,
        "diagnosis_codes": c.diagnosis_codes,
        "procedure_codes": c.procedure_codes,
        "treating_doctor": c.treating_doctor,
        "claimed_amount": str(c.claimed_amount) if c.claimed_amount is not None else None,
        "filing_deadline": _iso(c.filing_deadline),
        "insurer_claim_no": c.insurer_claim_no,
        "assigned_to": {"id": str(assignee.id), "name": assignee.display_name}
        if assignee
        else None,
        "completeness": completeness,
        "config_versions": c.config_versions,
        "allowed_transitions": transitions.allowed_for(c.status_t, p.roles),
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise ApiError("precondition_failed", "If-Match header with the case version is required")
    try:
        return int(value.strip().strip('"'))
    except ValueError:
        raise ApiError("precondition_failed", "If-Match must be the integer case version") from None


async def patch_case(
    uow: UoW, p: Principal, case_id: str, body: CasePatch, if_match: str | None, settings: Settings
) -> dict[str, Any]:
    expected = parse_if_match(if_match)
    c = await load_case(uow, p, case_id, for_update=True)
    if c.version != expected:
        raise ApiError("precondition_failed", "case was modified by someone else; reload")
    fields = body.model_dump(exclude_unset=True)
    status = c.status_t
    if status not in EDITABLE_STATUSES and not (
        status == "building_claim" and set(fields) <= {"treating_doctor"}
    ):
        raise ApiError("invalid_state", f"case in status {status} cannot be edited")
    if "claimed_amount" in fields:
        if status != "draft":
            raise ApiError("invalid_state", "claimed_amount is editable only in draft")
        try:
            amt = Decimal(fields["claimed_amount"])
            if amt < 0 or amt != amt.quantize(Decimal("0.01")):
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            raise ApiError(
                "validation_error", "claimed_amount must be a non-negative amount with 2 decimals"
            ) from None
        fields["claimed_amount"] = amt
    ad = fields.get("admitted_on", c.admitted_on)
    dd = fields.get("discharged_on", c.discharged_on)
    if ad and dd and dd < ad:
        raise ApiError("validation_error", "discharged_on must be on or after admitted_on")
    s = uow.session
    patient_f, policy_f = fields.pop("patient", None), fields.pop("policy", None)
    if fields:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        await s.execute(
            text(f"UPDATE claim_case SET {sets} WHERE id = :id"), {**fields, "id": c.id}
        )  # noqa: S608
    if patient_f:
        if "phone" in patient_f:
            ph = patient_f.pop("phone")
            if ph:
                patient_f["phone_enc"] = crypto.encrypt(ph, settings.field_key_b64)
        if patient_f:
            sets = ", ".join(f"{k} = :{k}" for k in patient_f)
            await s.execute(
                text(f"UPDATE patient SET {sets} WHERE id = :id"), {**patient_f, "id": c.patient_id}
            )  # noqa: S608
    if policy_f:
        sets = ", ".join(f"{k} = :{k}" for k in policy_f)
        await s.execute(
            text(f"UPDATE insurance_policy_ref SET {sets} WHERE id = :id"),  # noqa: S608
            {**policy_f, "id": c.policy_ref_id},
        )
    await s.execute(
        text("UPDATE claim_case SET version = version + 1 WHERE id = :id"), {"id": c.id}
    )
    await audit.append(
        s,
        c.id,
        "case.updated",
        {
            "fields": sorted(
                list(fields) + (["patient"] if patient_f else []) + (["policy"] if policy_f else [])
            )
        },
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return await get_case(uow, p, case_id, settings)


async def assign_case(uow: UoW, p: Principal, case_id: str, user_id: str | None) -> dict[str, Any]:
    c = await load_case(uow, p, case_id, for_update=True)
    s = uow.session
    target = None
    if user_id is not None:
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            raise ApiError("validation_error", "user_id must be a uuid or null") from None
        target = (
            await s.execute(text("SELECT id, role, active FROM app_user WHERE id = :i"), {"i": uid})
        ).first()
        if target is None or not target.active or target.role not in ("desk", "officer"):
            raise ApiError("validation_error", "assignee must be an active desk or officer user")
    await s.execute(
        text("UPDATE claim_case SET assigned_to = :a, version = version + 1 WHERE id = :i"),
        {"a": target.id if target else None, "i": c.id},
    )
    await audit.append(
        s, c.id, "case.assigned", {"assigned_to": user_id}, actor_type="human", actor_id=p.actor_id
    )
    await uow.commit()
    return {"id": str(c.id), "assigned_to": user_id, "version": c.version + 1}


async def timeline(
    uow: UoW, p: Principal, case_id: str, cursor: str | None, size: int
) -> dict[str, Any]:
    c = await load_case(uow, p, case_id)
    params: dict[str, Any] = {"c": c.id, "limit": size + 1}
    cur = ""
    if cursor:
        ts, _ = decode_cursor(cursor)
        cur = "WHERE ts < :cts"
        params["cts"] = ts
    sql = f"""
    SELECT * FROM (
      SELECT h.created_at AS ts, 'status' AS kind, h.from_status::text AS a, h.to_status::text AS b, h.actor_id, h.id
        FROM case_status_history h WHERE h.case_id = :c
      UNION ALL
      SELECT e.ts, 'audit', e.event_type, NULL, e.actor_id, e.id FROM audit_event e WHERE e.case_id = :c
    ) t {cur} ORDER BY ts DESC, id DESC LIMIT :limit"""  # noqa: S608
    rows = (await uow.session.execute(text(sql), params)).all()
    page = rows[:size]
    events = [
        {
            "ts": _iso(r.ts),
            "kind": r.kind,
            **({"from": r.a, "to": r.b} if r.kind == "status" else {"type": r.a}),
            "actor": r.actor_id,
        }
        for r in page
    ]
    nxt = encode_cursor(page[-1].ts, page[-1].id) if len(rows) > size and page else None
    return {"events": events, "next_cursor": nxt}
