"""Router persistence shell: gather facts, decide, store history/diff, overrides, ack, conversion."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from claim_contract.audit import canonical_json
from sqlalchemy import text

from app.auth.principal import Principal
from app.completeness.procedures import derive_procedure_group
from app.core.errors import ApiError
from app.core.uow import UoW
from app.router_engine.decide import decide
from app.router_engine.preauth import preauth_check
from app.services import audit, config_service, transitions

IST = ZoneInfo("Asia/Kolkata")
PRE_SUBMIT = {"draft", "docs_pending", "docs_complete", "building_claim", "ready_for_review"}
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
EVIDENCE_FLAGS = {"medico_legal": "fir_mlc"}
CONVERTIBLE = PRE_SUBMIT | {"submitted", "acknowledged", "under_query", "rejected"}
NOTE_TYPES = {"fir_mlc", "admission_note", "preauth_approval"}


def local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), IST).astimezone(UTC)


def _tz(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


_LOAD = (
    "SELECT c.*, c.status::text AS status_t, c.claim_type::text AS claim_type_t, "
    "c.admission_type::text AS admission_type_t FROM claim_case c WHERE c.id=:i"
)
_LOAD_FOR_UPDATE = _LOAD + " FOR NO KEY UPDATE"


async def load(uow: UoW, case_id: Any, lock: bool = True) -> Any:
    row = (
        await uow.session.execute(
            text(_LOAD_FOR_UPDATE if lock else _LOAD), {"i": uuid.UUID(str(case_id))}
        )
    ).first()
    if row is None:
        raise ApiError("not_found", "unknown case")
    return row


async def rules_for(uow: UoW, case: Any) -> tuple[int, dict[str, Any]]:
    ver = (case.config_versions or {}).get("router_rules")
    try:
        if ver is None:
            return await config_service.resolve(uow.session, "router_rules")
        return int(ver), await config_service.load_version(uow.session, "router_rules", int(ver))
    except config_service.ConfigNotFound:
        raise ApiError(
            "config_unavailable", "router_rules configuration is not available"
        ) from None


async def gather_facts(uow: UoW, case: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (facts, aux) where aux carries data for warnings (network known, preauth result)."""
    s = uow.session
    pol = (
        await s.execute(
            text("SELECT insurer_name, member_id FROM insurance_policy_ref WHERE id=:i"),
            {"i": case.policy_ref_id},
        )
    ).one()
    net = (
        await s.execute(
            text(
                "SELECT cashless_supported FROM network_insurer WHERE lower(insurer_name)=lower(:n)"
            ),
            {"n": pol.insurer_name},
        )
    ).scalar()
    docs = (
        await s.execute(
            text(
                "SELECT d.doc_type::text AS dtype, (SELECT jsonb_object_agg(k, v) FROM (SELECT (jsonb_each(p.typed_json)).* FROM "
                "document_parse p WHERE p.document_id = d.id ORDER BY p.pass_no) x(k, v) WHERE jsonb_typeof(v) <> 'null') AS j FROM document d "
                "WHERE d.case_id=:c AND d.lifecycle='active' AND d.scan_status='clean' AND d.doc_type IS NOT NULL"
            ),
            {"c": case.id},
        )
    ).all()
    types = {r.dtype for r in docs}
    note_flags: set[str] | None = None
    for r in docs:
        if r.dtype == "admission_note":
            note_flags = note_flags or set()
            j = r.j or {}
            if j.get("contains_emergency") is True or any(
                "emergency" in str(v).lower() for v in j.values() if isinstance(v, str)
            ):
                note_flags.add("contains_emergency")
    admitted = _tz(case.admitted_at)
    approx = False
    if admitted is None and case.admitted_on:
        admitted, approx = local_midnight(case.admitted_on), True
    discharged = _tz(case.discharged_at) or (
        local_midnight(case.discharged_on) if case.discharged_on else None
    )
    stay = (
        (case.discharged_on - case.admitted_on).days
        if case.admitted_on and case.discharged_on
        else None
    )
    codes = list(case.procedure_codes or [])
    group: str | None = None
    if codes:
        ver = (case.config_versions or {}).get("doc_requirements")
        payload = (
            await config_service.load_version(s, "doc_requirements", int(ver))
            if ver is not None
            else (await config_service.resolve(s, "doc_requirements"))[1]
        )
        group = derive_procedure_group(codes, payload.get("procedure_groups", {})) or "none"
    claimed = case.claimed_amount
    draft = (
        await s.execute(
            text("SELECT payload FROM claim_draft WHERE case_id=:c ORDER BY version DESC LIMIT 1"),
            {"c": case.id},
        )
    ).scalar()
    if draft:
        try:
            claimed = Decimal(str(draft["totals"]["claimed"]["amount"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            pass
    proposal = (case.route or {}).get("proposal") or {}
    pre = None
    pre_res = None
    if case.preauth_ref:
        pre = (
            await s.execute(
                text("SELECT * FROM simulated_preauth WHERE ref=:r"), {"r": case.preauth_ref}
            )
        ).first()
        pre_res = preauth_check(pre, pol.member_id, case.admitted_on, claimed)
    facts: dict[str, Any] = {
        "claim_type_proposed": proposal.get("claim_type", case.claim_type_t),
        "preauth_ref_present": bool(case.preauth_ref),
        "preauth_valid": None if pre_res is None else pre_res.state == "ok",
        "hospital_network": bool(net) if net is not None else False,
        "admission_source": case.admission_source,
        "admission_note_text_flags": note_flags,
        "stay_days": stay,
        "icd10_codes": list(case.diagnosis_codes) if case.diagnosis_codes else None,
        "procedure_codes": codes or None,
        "procedure_group": group,  # None = no codes yet (unknown); "none" = codes but no group (definite no)
        "claimed_amount": claimed,
        "doc_types_present": types,
        "preauth_issue": bool(pre_res and pre_res.state in ("issues", "not_found")),
        "admitted_at": admitted,
        "discharged_at": discharged,
        "admitted_at_estimated": approx,
    }
    return facts, {
        "network_known": net is not None,
        "preauth": pre_res,
        "insurer": pol.insurer_name,
        "proposal": proposal,
        "preauth_row": pre,
    }


def build_warnings(
    decision: Any,
    facts: dict[str, Any],
    aux: dict[str, Any],
    overrides: dict[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    w: list[dict[str, Any]] = []
    prop = aux["proposal"]
    if prop.get("claim_type") and prop["claim_type"] != decision.pipeline and not overrides:
        w.append(
            {
                "code": "claim_type_disagrees_with_selection",
                "needs_ack": True,
                "message": f"Desk selected {prop['claim_type']}; rules say {decision.pipeline}.",
            }
        )
    if prop.get("admission_type") == "planned" and decision.admission_type == "emergency":
        w.append(
            {
                "code": "emergency_conflicts_with_selection",
                "needs_ack": True,
                "message": "Documents indicate an emergency admission although planned was selected.",
            }
        )
    if not aux["network_known"]:
        w.append(
            {
                "code": "insurer_not_in_network_table",
                "needs_ack": False,
                "message": f"Insurer '{aux['insurer']}' is not in the cashless network table; defaulting to reimbursement.",
            }
        )
    if decision.pipeline == "cashless":
        pre = aux["preauth"]
        if pre is None:
            if decision.admission_type == "planned":
                w.append(
                    {
                        "code": "preauth_missing",
                        "needs_ack": False,
                        "field": "preauth_ref",
                        "message": "No pre-authorisation reference was entered.",
                    }
                )
        elif pre.state == "not_found":
            w.append(
                {
                    "code": "preauth_not_found",
                    "needs_ack": False,
                    "field": "preauth_ref",
                    "message": pre.warn,
                }
            )
        elif pre.state == "issues":
            for i in pre.issues:
                w.append(
                    {
                        "code": f"preauth_{i}",
                        "needs_ack": False,
                        "field": "preauth_ref",
                        "message": pre.warn,
                    }
                )
        if decision.preauth_by and (pre is None or pre.state != "ok"):
            by = datetime.strptime(decision.preauth_by, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            admitted = facts.get("admitted_at")
            if (
                by < now and admitted is not None and admitted > now
            ):  # moot once the patient is admitted
                w.append(
                    {
                        "code": "preauth_lead_time_missed",
                        "needs_ack": False,
                        "message": "Planned admission without a valid pre-auth inside the lead time.",
                    }
                )
    if (
        overrides
        and overrides.get("claim_type") == "cashless"
        and not facts.get("hospital_network")
    ):
        w.append(
            {
                "code": "override_against_network",
                "needs_ack": False,
                "message": "Cashless was set by override although the insurer is not in the cashless network.",
            }
        )
    if "late_filing" in decision.flags:
        w.append(
            {
                "code": "late_filing",
                "needs_ack": True,
                "message": "The reimbursement filing window has passed.",
            }
        )
    return sorted(w, key=lambda x: x["code"])


def diff(prev: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return {
            "flags": {"added": new["flags"], "removed": []},
            "required_steps": {"added": new["required_steps"], "removed": []},
            "pipeline": {"from": None, "to": new["pipeline"]},
        }
    pf, nf = set(prev["flags"]), set(new["flags"])
    ps, ns = prev["required_steps"], new["required_steps"]
    return {
        "flags": {"added": sorted(nf - pf), "removed": sorted(pf - nf)},
        "required_steps": {
            "added": [x for x in ns if x not in ps],
            "removed": [x for x in ps if x not in ns],
        },
        "pipeline": None
        if prev["pipeline"] == new["pipeline"]
        else {"from": prev["pipeline"], "to": new["pipeline"]},
    }


async def recompute(
    uow: UoW,
    case_id: Any,
    trigger: str,
    actor: str,
    *,
    hub: Any = None,
    completeness: Any = None,
    clear_overrides: bool = False,
    commit: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    s = uow.session
    case = await load(uow, case_id)
    now = now or datetime.now(UTC)
    _, rules = await rules_for(uow, case)
    dl_ver = (case.config_versions or {}).get("deadlines")
    dl = (
        await config_service.load_version(s, "deadlines", int(dl_ver))
        if dl_ver is not None
        else (await config_service.resolve(s, "deadlines"))[1]
    )
    facts, aux = await gather_facts(uow, case)
    route = dict(case.route or {})
    overrides = None if clear_overrides else route.get("overrides")
    decision = decide(facts, rules, overrides, dl, now)
    post = case.status_t in POST_SUBMIT
    prev_row = (
        await s.execute(
            text(
                "SELECT seq, decision, decision_hash FROM route_history WHERE case_id=:c "
                "ORDER BY seq DESC LIMIT 1"
            ),
            {"c": case.id},
        )
    ).first()
    d = decision.to_dict()
    if (
        post and prev_row
    ):  # after submission the steps and pipeline are frozen; flags still follow the facts
        d["pipeline"], d["required_steps"] = (
            prev_row.decision["decision"]["pipeline"],
            prev_row.decision["decision"]["required_steps"],
        )
        d["admission_type"] = prev_row.decision["decision"]["admission_type"]
        trigger = "post_submission"
    warnings = build_warnings(decision, facts, aux, overrides, now)
    stable = {
        "decision": {k: v for k, v in d.items() if k != "reminders"},
        "warnings": warnings,
        "overrides": overrides,
    }
    h = hashlib.sha256(canonical_json(stable).encode()).hexdigest()
    if (
        prev_row
        and prev_row.decision_hash == h
        and not (clear_overrides and route.get("overrides"))
    ):
        if commit:
            await uow.rollback()
        return {"changed": False, "seq": prev_row.seq}
    seq = (prev_row.seq + 1) if prev_row else 1
    await s.execute(
        text(
            "INSERT INTO route_history (id, case_id, seq, decision, decision_hash, trigger, actor) VALUES "
            "(uuid_generate_v7(), :c, :n, CAST(:d AS jsonb), :h, :t, :a)"
        ),
        {"c": case.id, "n": seq, "d": json.dumps(stable), "h": h, "t": trigger, "a": actor},
    )
    route.update({"decision": d, "warnings": warnings, "seq": seq, "provisional": d["provisional"]})
    if clear_overrides:
        route.pop("overrides", None)
    if "proposal" not in route:
        route["proposal"] = aux["proposal"] or {"claim_type": case.claim_type_t}
    fdate = None
    if d["filing_deadline"]:
        fdate = (
            datetime.strptime(d["filing_deadline"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .astimezone(IST)
            .date()
        )
    sets = {
        "route": json.dumps(route),
        "flags": d["flags"],
        "pg": None if facts.get("procedure_group") in (None, "none") else facts["procedure_group"],
        "inti": datetime.strptime(d["intimation_deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        if d["intimation_deadline"]
        else None,
        "fd": fdate,
        "id": case.id,
    }
    await s.execute(
        text(
            "UPDATE claim_case SET route=CAST(:route AS jsonb), flags=:flags, procedure_group=:pg, intimation_deadline=:inti, "
            "filing_deadline=:fd WHERE id=:id"
        ),
        sets,
    )
    if not post:  # the router decides the pipeline; the stored enum columns follow it
        await s.execute(
            text(
                "UPDATE claim_case SET claim_type=CAST(:ct AS claim_type), admission_type=CAST(:at AS admission_type) "
                "WHERE id=:id"
            ),
            {"ct": d["pipeline"], "at": d["admission_type"], "id": case.id},
        )
    delta = diff(prev_row.decision["decision"] if prev_row else None, d)
    await audit.append(
        s,
        case.id,
        "router.decided",
        {
            "seq": seq,
            "trigger": trigger,
            "pipeline": d["pipeline"],
            "admission_type": d["admission_type"],
            "flags": d["flags"],
            "provisional": d["provisional"],
        },
        actor_type="human" if len(actor) > 20 else "system",
        actor_id=actor,
        config_versions=case.config_versions or {},
    )
    changed_shape = bool(delta["pipeline"] or delta["flags"]["added"] or delta["flags"]["removed"])
    if hub is not None:

        async def pub() -> None:
            await hub.publish(
                "case.route_changed",
                str(case.id),
                {"seq": seq, "pipeline": d["pipeline"], "flags": d["flags"]},
            )

        uow.after_commit(pub)
    if commit:
        await uow.commit()
    if (
        commit and completeness is not None and changed_shape and not post
    ):  # never inside the caller's open transaction
        await completeness(str(case.id), "doc_event")
    return {
        "changed": True,
        "from_seq": prev_row.seq if prev_row else None,
        "to_seq": seq,
        "diff": delta,
        "completeness_run_scheduled": changed_shape and not post,
    }


async def get_route(uow: UoW, case_id: Any) -> dict[str, Any]:
    case = await load(uow, case_id, lock=False)
    r = case.route or {}
    if not r.get("decision"):
        raise ApiError("not_found", "no routing decision yet")
    return {
        "case_id": str(case.id),
        "seq": r.get("seq"),
        "provisional": r.get("provisional", False),
        "decision": {**r["decision"], "config_versions": case.config_versions},
        "warnings": r.get("warnings", []),
        "proposal": r.get("proposal"),
        "overrides": r.get("overrides"),
        "ack": r.get("ack"),
    }


async def override(
    uow: UoW, p: Principal, case_id: Any, body: dict[str, Any], hub: Any, completeness: Any
) -> dict[str, Any]:
    case = await load(uow, case_id)
    if case.status_t not in PRE_SUBMIT:
        raise ApiError(
            "case_locked",
            f"routing cannot be overridden while the case is {case.status_t}",
            status=409,  # doc 05 specifies 409 here (doc 03 uses 423 for uploads)
        )
    reason = (body.get("reason") or "").strip()
    if len(reason) < 15:
        raise ApiError(
            "reason_too_short", "an override reason of at least 15 characters is required"
        )
    allowed = {"claim_type", "admission_type", "flags_add", "flags_remove", "reason"}
    if set(body) - allowed:
        raise ApiError("validation_error", f"unknown override fields {sorted(set(body) - allowed)}")
    ov = {k: v for k, v in body.items() if k in allowed - {"reason"} and v not in (None, [])}
    if not ov:
        raise ApiError("validation_error", "nothing to override")
    if ov.get("claim_type") not in (None, "cashless", "reimbursement") or ov.get(
        "admission_type"
    ) not in (None, "planned", "emergency"):
        raise ApiError("validation_error", "invalid claim_type or admission_type")
    _, rules = await rules_for(uow, case)
    if ov.get("claim_type") and ov["claim_type"] not in rules["step_templates"]:
        raise ApiError("validation_error", "no step template exists for that claim type")
    facts, _ = await gather_facts(uow, case)
    for flag in ov.get("flags_remove", []):
        need = EVIDENCE_FLAGS.get(flag)
        if need and need in facts["doc_types_present"]:
            raise ApiError(
                "override_conflicts_evidence", f"cannot remove {flag}: a {need} document is present"
            )
    route = dict(case.route or {})
    old = route.get("overrides") or {}
    merged = {
        **old,
        **ov,
        "by": str(p.id),
        "reason": reason,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    route["overrides"] = merged
    await uow.session.execute(
        text("UPDATE claim_case SET route = CAST(:r AS jsonb) WHERE id=:i"),
        {"r": json.dumps(route), "i": case.id},
    )
    await audit.append(
        uow.session,
        case.id,
        "router.override",
        {"override": {k: v for k, v in ov.items()}, "reason": reason},
        actor_type="human",
        actor_id=p.actor_id,
    )
    out = await recompute(
        uow, case.id, "override", p.actor_id, hub=hub, completeness=None, commit=False
    )
    await uow.commit()
    if completeness is not None:
        await completeness(str(case.id), "doc_event")
    r = await get_route(uow, case.id)
    return {**r, "override": True, "recompute": out}


async def acknowledge(
    uow: UoW, p: Principal, case_id: Any, codes: list[str] | None
) -> dict[str, Any]:
    case = await load(uow, case_id)
    route = dict(case.route or {})
    need = [w["code"] for w in route.get("warnings", []) if w.get("needs_ack")]
    if not need:
        raise ApiError("invalid_state", "nothing needs acknowledgement")
    codes = codes or need
    unknown = set(codes) - set(need)
    if unknown:
        raise ApiError("validation_error", f"not acknowledgeable: {sorted(unknown)}")
    ack = route.get("ack") or {"codes": []}
    ack = {
        "codes": sorted(set(ack["codes"]) | set(codes)),
        "by": str(p.id),
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    route["ack"] = ack
    await uow.session.execute(
        text("UPDATE claim_case SET route = CAST(:r AS jsonb) WHERE id=:i"),
        {"r": json.dumps(route), "i": case.id},
    )
    await audit.append(
        uow.session,
        case.id,
        "router.ack",
        {"codes": ack["codes"]},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    return {"ack": ack, "pending": sorted(set(need) - set(ack["codes"]))}


async def convert(
    uow: UoW,
    p: Principal,
    case_id: Any,
    to: str,
    reason: str,
    store: Any,
    hub: Any,
    completeness: Any,
) -> dict[str, Any]:
    if to != "reimbursement":
        raise ApiError("validation_error", "only conversion to reimbursement is supported")
    reason = (reason or "").strip()
    if len(reason) < 15:
        raise ApiError(
            "reason_too_short", "a conversion reason of at least 15 characters is required"
        )
    s = uow.session
    case = await load(uow, case_id)
    if case.converted_to is not None:
        succ = (
            await s.execute(
                text("SELECT claim_ref FROM claim_case WHERE id=:i"), {"i": case.converted_to}
            )
        ).scalar()
        raise ApiError(
            "already_converted",
            f"already converted to {succ}",
            successor=str(case.converted_to),
            successor_claim_ref=succ,
        )
    if case.status_t not in CONVERTIBLE or case.claim_type_t != "cashless":
        raise ApiError(
            "invalid_state",
            f"a {case.claim_type_t} case in status {case.status_t} cannot be converted",
        )
    cfg = await config_service.snapshot(s)
    ref = (await s.execute(text("SELECT next_claim_ref()"))).scalar()
    new_route = {
        "proposal": {
            "claim_type": "reimbursement",
            "admission_type": case.admission_type_t,
            "by": str(p.id),
        },
        "converted_from": case.claim_ref,
    }
    new_id = (
        await s.execute(
            text(
                "INSERT INTO claim_case (id, claim_ref, hospital_id, patient_id, policy_ref_id, claim_type, admission_type, "
                "admitted_on, admitted_at, discharged_on, discharged_at, diagnosis_codes, procedure_codes, treating_doctor, "
                "claimed_amount, admission_source, config_versions, created_by, converted_from, route) VALUES (uuid_generate_v7(), "
                ":ref, :h, :p, :pol, 'reimbursement', CAST(:at AS admission_type), :ad, :ada, :dd, :dda, :dx, :px, :doc, :amt, "
                ":src, CAST(:cv AS jsonb), :u, :orig, CAST(:r AS jsonb)) RETURNING id"
            ),
            {
                "ref": ref,
                "h": case.hospital_id,
                "p": case.patient_id,
                "pol": case.policy_ref_id,
                "at": case.admission_type_t,
                "ad": case.admitted_on,
                "ada": case.admitted_at,
                "dd": case.discharged_on,
                "dda": case.discharged_at,
                "dx": case.diagnosis_codes,
                "px": case.procedure_codes,
                "doc": case.treating_doctor,
                "amt": case.claimed_amount,
                "src": case.admission_source,
                "cv": json.dumps(cfg),
                "u": p.id,
                "orig": case.id,
                "r": json.dumps(new_route),
            },
        )
    ).scalar()
    await s.execute(
        text(
            "INSERT INTO case_status_history (id, case_id, from_status, to_status, actor_id, reason) VALUES "
            "(uuid_generate_v7(), :c, NULL, 'draft', :a, :r)"
        ),
        {"c": new_id, "a": p.actor_id, "r": f"converted from {case.claim_ref}"},
    )
    docs = (
        (
            await s.execute(
                text(
                    "SELECT * FROM document WHERE case_id=:c AND lifecycle='active' AND scan_status='clean' "
                    "AND storage_key IS NOT NULL ORDER BY created_at"
                ),
                {"c": case.id},
            )
        )
        .mappings()
        .all()
    )
    copied = 0
    for d in docs:
        new_doc = uuid.uuid4()
        ext = d["storage_key"].rsplit(".", 1)[-1]
        key = f"{new_id}/{new_doc}/original.{ext}"
        await store.copy(
            d["storage_key"], key
        )  # a real copy: deleting in one case must never break the other
        await s.execute(
            text(
                "INSERT INTO document (id, case_id, original_filename, mime_type, size_bytes, sha256, storage_key, pages, scan_status, "
                "lifecycle, doc_type, doc_type_source, classification_confidence, parse_status, parse_confidence, quality_score, "
                "quality_flags, has_required_stamp, uploaded_by) VALUES (:id, :c, :n, :m, :sz, :sha, :k, :pg, 'clean', 'active', "
                "CAST(:dt AS doc_type), :src, :cc, :ps, :pc, :qs, :qf, :st, :u)"
            ),
            {
                "id": new_doc,
                "c": new_id,
                "n": d["original_filename"],
                "m": d["mime_type"],
                "sz": d["size_bytes"],
                "sha": d["sha256"],
                "k": key,
                "pg": d["pages"],
                "dt": str(d["doc_type"]) if d["doc_type"] else None,
                "src": d["doc_type_source"],
                "cc": d["classification_confidence"],
                "ps": d["parse_status"],
                "pc": d["parse_confidence"],
                "qs": d["quality_score"],
                "qf": d["quality_flags"],
                "st": d["has_required_stamp"],
                "u": p.id,
            },
        )
        await s.execute(
            text(
                "INSERT INTO document_parse (id, document_id, pass_no, engine, engine_version, raw_markdown_key, masked_text_key, "
                "entities, typed_json, confidence, agreement_score, duration_ms) SELECT uuid_generate_v7(), :nd, pass_no, engine, "
                "engine_version, raw_markdown_key, masked_text_key, entities, typed_json, confidence, agreement_score, duration_ms "
                "FROM document_parse WHERE document_id=:od"
            ),
            {"nd": new_doc, "od": d["id"]},
        )
        copied += 1
    await s.execute(
        text("UPDATE claim_case SET converted_to=:n WHERE id=:o"), {"n": new_id, "o": case.id}
    )
    await transitions.transition(uow, case.id, "closed", p, reason="converted", hub=hub)
    await audit.append(
        s,
        case.id,
        "router.converted",
        {"to_case": str(new_id), "to_ref": ref, "reason": reason},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await audit.append(
        s,
        new_id,
        "router.converted",
        {
            "from_case": str(case.id),
            "from_ref": case.claim_ref,
            "reason": reason,
            "documents_copied": copied,
        },
        actor_type="human",
        actor_id=p.actor_id,
    )
    await recompute(uow, new_id, "create", p.actor_id, hub=hub, commit=False)
    await uow.commit()
    if completeness is not None:
        await completeness(str(new_id), "doc_event")
    return {
        "new_case_id": str(new_id),
        "new_claim_ref": ref,
        "linked_from": case.claim_ref,
        "documents_copied": copied,
        "original_status": "closed",
        "original_close_reason": "converted",
    }


async def count_open_cases(uow: UoW) -> int:
    return int(
        (
            await uow.session.execute(
                text("SELECT count(*) FROM claim_case WHERE status IN ('draft','docs_pending')")
            )
        ).scalar()
        or 0
    )


async def recompute_open_cases_job(app: Any, new_version: int) -> int:
    """Background job after router_rules is republished: in-flight draft/docs_pending cases move to the new
    version and are recomputed, newest first, one short transaction each (doc 05 §5.5). Submitted cases are
    never touched."""
    st = app.state
    async with st.sessionmaker() as s:
        ids = [
            r.id
            for r in (
                await s.execute(
                    text(
                        "SELECT id FROM claim_case WHERE status IN ('draft','docs_pending') "
                        "ORDER BY updated_at DESC"
                    )
                )
            ).all()
        ]
    done = 0
    for cid in ids:
        try:
            async with st.sessionmaker() as s:
                uow = UoW(s)
                await s.execute(
                    text(
                        "UPDATE claim_case SET config_versions = jsonb_set(COALESCE(config_versions, '{}'::jsonb), "
                        "'{router_rules}', to_jsonb(CAST(:v AS int))) WHERE id=:i AND status IN ('draft','docs_pending')"
                    ),
                    {"v": new_version, "i": cid},
                )
                await s.commit()
                await recompute(
                    uow, cid, "config_republish", "system", hub=st.hub, completeness=st.completeness
                )
            done += 1
        except Exception:  # noqa: BLE001  one bad case must not stop the rest
            import logging

            logging.getLogger("app.router").exception("recompute failed for %s", cid)
    return done
