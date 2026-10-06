"""Completeness shell (doc 04 §5): load context, evaluate (pure), persist, reconcile needs-info requests,
drive case status, schedule reminders. All DB writes happen in one transaction per run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from claim_contract.audit import canonical_json
from sqlalchemy import text

from app.auth.principal import Principal
from app.completeness import messages
from app.completeness.context import (
    AdmissionFacts,
    CaseContext,
    DocFacts,
    PatientFacts,
    WaiverFacts,
)
from app.completeness.procedures import derive_procedure_group, has_surgery
from app.completeness.rules import evaluate
from app.completeness.schemas import DocRequirementsConfig, Result
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import audit, config_service, transitions

log = logging.getLogger("app.completeness")
TRIGGERS = {
    "doc_event",
    "manual",
    "waive",
    "reclassify",
    "nightly",
    "config_republish",
    "claim_build_precheck",
}
NON_WAIVABLE = {"prescription", "pharmacy_bill", "final_bill"}
REVIEWABLE_STATUSES = ("docs_pending", "docs_complete", "ready_for_review")


def _tz(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


async def load_config(uow: UoW, case: Any) -> tuple[int, DocRequirementsConfig]:
    ver = (case.config_versions or {}).get("doc_requirements")
    try:
        if ver is None:
            ver, payload = await config_service.resolve(uow.session, "doc_requirements")
        else:
            payload = await config_service.load_version(uow.session, "doc_requirements", int(ver))
    except config_service.ConfigNotFound:
        raise ApiError(
            "config_unavailable", "doc_requirements configuration is not available"
        ) from None
    return int(ver), DocRequirementsConfig.model_validate(payload)


async def build_context(
    uow: UoW, case: Any, cfg: DocRequirementsConfig, now: datetime, name_match_min: int = 90
) -> CaseContext:
    s = uow.session
    pat = (
        await s.execute(
            text("SELECT full_name, dob FROM patient WHERE id=:i"), {"i": case.patient_id}
        )
    ).one()
    pol = (
        await s.execute(
            text("SELECT member_id, policy_number FROM insurance_policy_ref WHERE id=:i"),
            {"i": case.policy_ref_id},
        )
    ).one()
    gates_ver = (case.config_versions or {}).get("confidence_gates")
    if gates_ver is not None:
        gates = await config_service.load_version(s, "confidence_gates", int(gates_ver))
    else:
        gates = (await config_service.resolve(s, "confidence_gates"))[1]
    rows = (
        await s.execute(
            text(
                "SELECT d.id, d.doc_type::text AS doc_type, d.lifecycle, d.scan_status, d.parse_status, d.quality_flags, "
                "d.quality_score, d.has_required_stamp, d.classification_confidence, d.pages, d.created_at, "
                "(SELECT jsonb_object_agg(k, v) FROM (SELECT (jsonb_each(p.typed_json)).* FROM document_parse p "
                "  WHERE p.document_id = d.id ORDER BY p.pass_no) t(k, v)) AS typed_json, "
                "(SELECT p.confidence FROM document_parse p WHERE p.document_id = d.id ORDER BY p.pass_no DESC LIMIT 1) "
                "  AS parse_conf, "
                "(SELECT p.agreement_score FROM document_parse p WHERE p.document_id = d.id AND p.agreement_score IS NOT NULL "
                "  ORDER BY p.pass_no DESC LIMIT 1) AS agreement "
                "FROM document d WHERE d.case_id = :c"
            ),
            {"c": case.id},
        )
    ).all()
    by: dict[str, list[DocFacts]] = {}
    unclassified: list[str] = []
    for r in rows:
        if r.scan_status == "infected" or r.lifecycle == "quarantined":
            state, reason = "excluded", "infected"
        elif r.lifecycle in ("superseded", "deleted"):
            state, reason = "excluded", r.lifecycle
        elif r.scan_status == "pending" or r.parse_status in ("pending", "processing"):
            state, reason = "pending_processing", None
        else:
            state, reason = "ok", None
        if r.doc_type is None:
            if state != "excluded":
                unclassified.append(str(r.id))
            continue
        pc = r.parse_conf
        if state == "ok" and pc is None:
            pc = 0.0  # parsing failed or produced nothing: needs a human, never silently "fine"
        d = DocFacts(
            doc_id=str(r.id),
            doc_type=r.doc_type,
            usable_state=state,
            uploaded_at=_tz(r.created_at),
            quality_flags=frozenset(r.quality_flags or []),
            quality_score=r.quality_score if r.quality_score is not None else 1.0,
            has_required_stamp=r.has_required_stamp,
            stamp_confidence=None,
            parse_confidence=pc,
            agreement_score=r.agreement,
            classification_confidence=r.classification_confidence,
            typed_json=r.typed_json,
            page_count=r.pages or 1,
            excluded_reason=reason,
        )
        by.setdefault(r.doc_type, []).append(d)
    waivers = {
        w.rule_id: WaiverFacts(w.rule_id, w.reason)
        for w in (
            await s.execute(
                text(
                    "SELECT rule_id, reason FROM requirement_waiver WHERE case_id=:c AND revoked_at IS NULL"
                ),
                {"c": case.id},
            )
        )
    }
    codes = list(case.procedure_codes or [])
    flags = set(case.flags or [])
    if has_surgery(codes):
        flags.add("has_surgery")
    group = derive_procedure_group(codes, cfg.procedure_groups) or case.procedure_group
    return CaseContext(
        case_id=str(case.id),
        claim_type=case.claim_type_t,
        admission_type=case.admission_type_t,
        procedure_group=group,
        flags=frozenset(flags),
        patient=PatientFacts(pat.full_name, pat.dob, pol.member_id, pol.policy_number),
        admission=AdmissionFacts(
            case.admitted_on, case.discharged_on, tuple(case.diagnosis_codes or ()), tuple(codes)
        ),
        docs_by_type=by,
        now=now,
        waivers=waivers,
        gates=gates,
        unclassified_ids=tuple(unclassified),
        name_match_min=name_match_min,
    )


def result_hash(result: Result) -> str:
    return hashlib.sha256(canonical_json(result.model_dump(mode="json")).encode()).hexdigest()


async def run(
    uow: UoW,
    case_id: uuid.UUID | str,
    trigger: str,
    *,
    hub: Any = None,
    settings: Any = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Evaluate a case and persist the outcome. Returns {run_no, complete, changed, status}."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown completeness trigger {trigger}")
    s = uow.session
    case = (
        await s.execute(
            text(
                "SELECT c.*, c.status::text AS status_t, c.claim_type::text AS claim_type_t, "
                "c.admission_type::text AS admission_type_t FROM claim_case c WHERE c.id=:i FOR UPDATE"
            ),
            {"i": uuid.UUID(str(case_id))},
        )
    ).first()
    if case is None:
        raise ApiError("not_found", "unknown case")
    now = now or datetime.now(UTC)
    cfg_ver, cfg = await load_config(uow, case)
    ctx = await build_context(uow, case, cfg, now, getattr(settings, "name_match_min", 90))
    result = evaluate(ctx, cfg)
    h = result_hash(result)
    prev = (
        await s.execute(
            text(
                "SELECT run_no, result_hash, complete FROM completeness_check WHERE case_id=:c "
                "ORDER BY run_no DESC LIMIT 1"
            ),
            {"c": case.id},
        )
    ).first()
    if prev and prev.result_hash == h and trigger != "manual" and not force:
        # same checklist: no new history row, but the case status may still be out of step with it (for example a
        # case pushed back to docs_pending by a failed claim build while its documents are still complete)
        has_docs = bool(ctx.docs_by_type or ctx.unclassified_ids)
        new_status = decide_status(case.status_t, result, has_docs=has_docs)
        if new_status and new_status != case.status_t:
            await transitions.transition(
                uow, case.id, new_status, None, reason="completeness", hub=hub
            )
            if case.status_t == "ready_for_review":
                await invalidate_signoffs(uow, case.id, "completeness found blockers")
            await uow.commit()
        else:
            await uow.rollback()
        return {
            "run_no": prev.run_no,
            "complete": prev.complete,
            "changed": False,
            "status": new_status or case.status_t,
        }
    run_no = (prev.run_no + 1) if prev else 1
    body = result.model_dump(mode="json")
    await s.execute(
        text(
            "INSERT INTO completeness_check (id, case_id, config_version, run_no, complete, result, trigger, provisional, "
            "blocker_count, warning_count, result_hash) VALUES (uuid_generate_v7(), :c, :v, :n, :ok, CAST(:r AS jsonb), "
            ":t, :pv, :b, :w, :h)"
        ),
        {
            "c": case.id,
            "v": cfg_ver,
            "n": run_no,
            "ok": result.complete,
            "r": json.dumps(body),
            "t": trigger,
            "pv": result.provisional,
            "b": result.blocker_count,
            "w": result.warning_count,
            "h": h,
        },
    )
    await reconcile_doc_requests(uow, case, result, run_no, now)
    await schedule_filing_reminders(uow, case, now)
    new_status = decide_status(
        case.status_t, result, has_docs=bool(ctx.docs_by_type or ctx.unclassified_ids)
    )
    if new_status and new_status != case.status_t:
        await transitions.transition(uow, case.id, new_status, None, reason="completeness", hub=hub)
        if case.status_t == "ready_for_review":
            await invalidate_signoffs(uow, case.id, "completeness found blockers")
    await audit.append(
        s,
        case.id,
        "completeness.evaluated",
        {
            "run_no": run_no,
            "complete": result.complete,
            "blockers": result.blocker_count,
            "warnings": result.warning_count,
            "config_version": cfg_ver,
            "trigger": trigger,
        },
        actor_id="completeness",
        config_versions={"doc_requirements": cfg_ver},
    )
    if hub is not None:

        async def publish() -> None:
            await hub.publish(
                "completeness.updated",
                str(case.id),
                {"run_no": run_no, "complete": result.complete},
            )

        uow.after_commit(publish)
    await uow.commit()
    return {
        "run_no": run_no,
        "complete": result.complete,
        "changed": True,
        "status": new_status or case.status_t,
    }


def decide_status(current: str, result: Result, has_docs: bool) -> str | None:
    if current in ("docs_pending",) and result.complete and has_docs:
        return "docs_complete"
    if current == "docs_complete" and not result.complete:
        return "docs_pending"
    if current == "ready_for_review" and not result.complete:
        return "docs_pending"
    return None


async def invalidate_signoffs(uow: UoW, case_id: Any, reason: str) -> int:
    res = await uow.session.execute(
        text(
            "UPDATE signoff SET invalidated_at = now(), invalidated_reason = :r WHERE case_id = :c AND invalidated_at IS NULL "
            "RETURNING id"
        ),
        {"c": case_id, "r": reason},
    )
    n = len(res.all())
    if n:
        await audit.append(
            uow.session, case_id, "signoff.invalidated", {"count": n, "reason": reason}
        )
    return n


async def deadlines(uow: UoW, case: Any) -> dict[str, Any]:
    v = (case.config_versions or {}).get("deadlines")
    if v is not None:
        return await config_service.load_version(uow.session, "deadlines", int(v))
    return (await config_service.resolve(uow.session, "deadlines"))[1]


async def reconcile_doc_requests(
    uow: UoW, case: Any, result: Result, run_no: int, now: datetime
) -> None:
    s = uow.session
    dl = await deadlines(uow, case)
    sla_h = dl.get("request_sla_hours", 72)
    offsets = dl.get("reminder_offsets_hours", [24, 48, 72])
    desired: dict[tuple[str, str], Any] = {}
    for it in result.items:
        if it.severity == "blocker" and it.status in ("missing", "unusable") and it.reasons:
            desired[(it.rule_id, it.reasons[0])] = it  # one request per (rule, primary reason)
    waived = {i.rule_id for i in result.items if i.status == "waived"}
    # the document that now satisfies a rule: the first document of any item that no longer blocks
    ok_docs = {
        i.rule_id: i.document_ids[0]
        for i in result.items
        if i.document_ids and i.severity != "blocker"
    }
    # still being processed: keep the request open rather than closing it half-way
    pending = {i.rule_id for i in result.items if i.status == "pending_processing"}
    open_rows = (
        await s.execute(
            text(
                "SELECT id, rule_id, reason_code, doc_type::text AS doc_type FROM doc_request "
                "WHERE case_id=:c AND status='open' FOR UPDATE"
            ),
            {"c": case.id},
        )
    ).all()
    existing = {(r.rule_id, r.reason_code): r for r in open_rows}
    closed = opened = 0
    for key, r in existing.items():
        if key in desired or r.rule_id in pending:
            await s.execute(
                text("UPDATE doc_request SET last_seen_run=:n WHERE id=:i"),
                {"n": run_no, "i": r.id},
            )
            continue
        if r.rule_id in waived:
            await s.execute(
                text(
                    "UPDATE doc_request SET status='waived', closed_at=now(), last_seen_run=:n, "
                    "waived_by=(SELECT waived_by FROM requirement_waiver WHERE case_id=:c AND rule_id=:r AND revoked_at IS NULL), "
                    "waive_reason=(SELECT reason FROM requirement_waiver WHERE case_id=:c AND rule_id=:r AND revoked_at IS NULL) "
                    "WHERE id=:i"
                ),
                {"n": run_no, "c": case.id, "r": r.rule_id, "i": r.id},
            )
        else:
            await s.execute(
                text(
                    "UPDATE doc_request SET status='fulfilled', closed_at=now(), fulfilled_by_doc=CAST(:d AS uuid), "
                    "last_seen_run=:n WHERE id=:i"
                ),
                {"d": ok_docs.get(r.rule_id), "n": run_no, "i": r.id},
            )
        await s.execute(
            text(
                "UPDATE reminder SET status='cancelled' WHERE kind='doc_request' AND ref_id=:i AND status='scheduled'"
            ),
            {"i": r.id},
        )
        closed += 1
    for (rule_id, reason), it in sorted(desired.items()):
        if (rule_id, reason) in existing:
            continue
        due = now + timedelta(hours=sla_h)
        rid = (
            await s.execute(
                text(
                    "INSERT INTO doc_request (id, case_id, doc_type, rule_id, reason, reason_code, status, due_by, opened_by_run, "
                    "last_seen_run) VALUES (uuid_generate_v7(), :c, CAST(:dt AS doc_type), :r, :m, :rc, 'open', :due, :n, :n) "
                    "RETURNING id"
                ),
                {
                    "c": case.id,
                    "dt": it.doc_type or "other",
                    "r": rule_id,
                    "m": it.message or messages.render(it.doc_type, reason),
                    "rc": reason,
                    "due": due,
                    "n": run_no,
                },
            )
        ).scalar()
        for h in offsets:
            fire = now + timedelta(hours=h)
            await s.execute(
                text(
                    "INSERT INTO reminder (id, case_id, kind, ref_id, channel, fire_at, payload) VALUES (uuid_generate_v7(), "
                    ":c, 'doc_request', :ref, 'in_app', :f, CAST(:p AS jsonb)) ON CONFLICT ON CONSTRAINT uq_reminder_dedup DO NOTHING"
                ),
                {
                    "c": case.id,
                    "ref": rid,
                    "f": fire,
                    "p": json.dumps({"rule_id": rule_id, "offset_h": h}),
                },
            )
        opened += 1
    if opened or closed:
        await audit.append(
            s,
            case.id,
            "doc_request.opened" if opened else "doc_request.closed",
            {"opened": opened, "closed": closed, "run_no": run_no},
            actor_id="completeness",
        )


async def schedule_filing_reminders(uow: UoW, case: Any, now: datetime) -> None:
    if case.claim_type_t != "reimbursement" or case.filing_deadline is None:
        return
    for days_before in (7, 2):
        fire = datetime.combine(
            case.filing_deadline - timedelta(days=days_before), time(9, 0), tzinfo=UTC
        )
        if fire <= now:
            continue
        await uow.session.execute(
            text(
                "INSERT INTO reminder (id, case_id, kind, fire_at, payload) VALUES (uuid_generate_v7(), :c, 'filing_deadline', "
                ":f, CAST(:p AS jsonb)) ON CONFLICT ON CONSTRAINT uq_reminder_dedup DO NOTHING"
            ),
            {"c": case.id, "f": fire, "p": json.dumps({"days_before": days_before})},
        )


# --------------------------------------------------------------------------------------- user-facing
async def latest(uow: UoW, case_id: Any, history: bool = False) -> dict[str, Any]:
    s = uow.session
    rows = (
        await s.execute(
            text(
                "SELECT run_no, created_at, provisional, complete, config_version, blocker_count, warning_count, result, trigger "
                "FROM completeness_check WHERE case_id=:c ORDER BY run_no DESC"
                + ("" if history else " LIMIT 1")
            ),
            {"c": case_id},
        )
    ).all()
    if not rows:
        raise ApiError("not_found", "no completeness run yet")
    r = rows[0]
    items = r.result["items"]
    summary = {
        "blockers": r.blocker_count,
        "warnings": r.warning_count,
        "needs_review": sum(1 for i in items if i["status"] == "needs_review"),
        "ok": sum(1 for i in items if i["status"] == "present_ok"),
        "waived": sum(1 for i in items if i["status"] == "waived"),
    }
    out = {
        "case_id": str(case_id),
        "run_no": r.run_no,
        "created_at": r.created_at.isoformat(),
        "provisional": r.provisional,
        "complete": r.complete,
        "config_version": r.config_version,
        "summary": summary,
        "items": items,
        "open_doc_requests": await open_requests(uow, case_id),
    }
    if history:
        out["history"] = [
            {
                "run_no": x.run_no,
                "created_at": x.created_at.isoformat(),
                "complete": x.complete,
                "blockers": x.blocker_count,
                "trigger": x.trigger,
            }
            for x in rows
        ]
    return out


async def open_requests(uow: UoW, case_id: Any) -> list[dict[str, Any]]:
    rows = (
        await uow.session.execute(
            text(
                "SELECT id, rule_id, reason_code, reason, doc_type::text AS doc_type, due_by, reminders_sent FROM doc_request "
                "WHERE case_id=:c AND status='open' ORDER BY rule_id, reason_code"
            ),
            {"c": case_id},
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "rule_id": r.rule_id,
            "reason_code": r.reason_code,
            "message": r.reason,
            "doc_type": r.doc_type,
            "due_by": r.due_by.isoformat() if r.due_by else None,
            "reminders_sent": r.reminders_sent,
        }
        for r in rows
    ]


async def waive(
    uow: UoW, p: Principal, case: Any, doc_type: str, reason: str, hub: Any, settings: Any
) -> dict[str, Any]:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ApiError("reason_too_short", "a waiver reason of at least 10 characters is required")
    cfg_ver, cfg = await load_config(uow, case)
    ctx = await build_context(uow, case, cfg, datetime.now(UTC))
    from app.completeness.rules import applies

    rule = next(
        (r for r in cfg.rules if r.doc_type.value == doc_type and applies(r.applies, ctx)), None
    )
    if rule is None:
        raise ApiError("unknown_rule", f"no applicable requirement for {doc_type} on this case")
    if doc_type in NON_WAIVABLE and not rule.waivable:
        raise ApiError(
            "not_waivable", f"{doc_type} cannot be waived unless the rule is configured waivable"
        )
    s = uow.session
    await s.execute(
        text(
            "INSERT INTO requirement_waiver (id, case_id, rule_id, doc_type, reason, waived_by) VALUES (uuid_generate_v7(), "
            ":c, :r, CAST(:d AS doc_type), :m, :u) ON CONFLICT (case_id, rule_id) WHERE revoked_at IS NULL DO NOTHING"
        ),
        {"c": case.id, "r": rule.id, "d": doc_type, "m": reason, "u": p.id},
    )
    await audit.append(
        s,
        case.id,
        "requirement.waived",
        {"rule_id": rule.id, "doc_type": doc_type},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    out = await run_with_new_uow(uow, case.id, "waive", hub, settings)
    return {
        "rule_id": rule.id,
        "status": "waived",
        "waived_by": str(p.id),
        "completeness_run_no": out["run_no"],
    }


async def revoke_waiver(
    uow: UoW, p: Principal, case: Any, doc_type: str, hub: Any, settings: Any
) -> dict[str, Any]:
    res = await uow.session.execute(
        text(
            "UPDATE requirement_waiver SET revoked_at=now(), revoked_by=:u WHERE case_id=:c AND doc_type=CAST(:d AS doc_type) "
            "AND revoked_at IS NULL RETURNING rule_id"
        ),
        {"u": p.id, "c": case.id, "d": doc_type},
    )
    rules = [r.rule_id for r in res.all()]
    if not rules:
        raise ApiError("not_found", "no active waiver for this requirement")
    await audit.append(
        uow.session,
        case.id,
        "requirement.waiver_revoked",
        {"rules": rules},
        actor_type="human",
        actor_id=p.actor_id,
    )
    await uow.commit()
    out = await run_with_new_uow(uow, case.id, "waive", hub, settings)
    return {"revoked": rules, "completeness_run_no": out["run_no"]}


async def run_with_new_uow(
    uow: UoW, case_id: Any, trigger: str, hub: Any, settings: Any
) -> dict[str, Any]:
    return await run(uow, case_id, trigger, hub=hub, settings=settings, force=True)


async def remind(
    uow: UoW, redis: Any, case_id: Any, request_id: str, p: Principal
) -> dict[str, Any]:
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise ApiError("not_found", "unknown doc request") from None
    r = (
        await uow.session.execute(
            text("SELECT id, status FROM doc_request WHERE id=:i AND case_id=:c"),
            {"i": rid, "c": case_id},
        )
    ).first()
    if r is None:
        raise ApiError("not_found", "unknown doc request")
    if r.status != "open":
        raise ApiError("invalid_state", "request is not open")
    if not await redis.set(f"cache:hosp:remind:{rid}", "1", nx=True, ex=3600):
        raise ApiError(
            "rate_limited",
            "a reminder was already sent in the last hour",
            headers={"Retry-After": "3600"},
        )
    await uow.session.execute(
        text(
            "INSERT INTO reminder (id, case_id, kind, ref_id, channel, fire_at, payload) VALUES (uuid_generate_v7(), :c, "
            "'doc_request', :r, 'in_app', now(), CAST(:p AS jsonb)) ON CONFLICT ON CONSTRAINT uq_reminder_dedup DO NOTHING"
        ),
        {"c": case_id, "r": rid, "p": json.dumps({"manual": True, "by": str(p.id)})},
    )
    await uow.session.execute(
        text("UPDATE doc_request SET reminders_sent = reminders_sent + 1 WHERE id=:i"), {"i": rid}
    )
    await uow.commit()
    return {"id": str(rid), "reminder": "scheduled"}


# ------------------------------------------------------------------------------------------ scheduler
class CompletenessScheduler:
    """Debounced trigger: the first event schedules a run after `debounce_s`; events arriving meanwhile set a
    dirty marker so one more run follows. With debounce 0 the run is executed inline (tests, manual runs)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.tasks: set[asyncio.Task[Any]] = set()

    async def schedule(self, case_id: str, reason: str) -> None:
        st = self.app.state
        trigger = reason if reason in TRIGGERS else "doc_event"
        delay = float(st.settings.completeness_debounce_s)
        if delay <= 0:
            await self._run(case_id, trigger)
            return
        key = f"cache:hosp:completeness:debounce:{case_id}"
        if await st.redis.set(key, "1", nx=True, ex=max(int(delay) + 55, 60)):
            await st.redis.set(f"{key}:dirty", "0", ex=120)
            t = asyncio.create_task(self._delayed(case_id, trigger, delay))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        else:
            await st.redis.set(f"{key}:dirty", "1", ex=120)

    async def _delayed(self, case_id: str, trigger: str, delay: float) -> None:
        await asyncio.sleep(delay)
        st = self.app.state
        key = f"cache:hosp:completeness:debounce:{case_id}"
        await st.redis.delete(key)
        try:
            await self._run(case_id, trigger)
        finally:
            if await st.redis.getdel(f"{key}:dirty") == "1":
                await self.schedule(case_id, "doc_event")

    async def _run(self, case_id: str, trigger: str) -> None:
        st = self.app.state
        async with st.sessionmaker() as session:
            try:
                await run(UoW(session), case_id, trigger, hub=st.hub, settings=st.settings)
            except ApiError as e:
                log.warning("completeness run for %s failed: %s", case_id, e.code)
            except Exception:  # noqa: BLE001
                log.exception("completeness run failed")

    def spawn(self, coro: Any) -> None:
        """Run a long job in the background (tracked so tests and shutdown can await it)."""
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


__all__ = ["CompletenessScheduler", "date", "run"]
