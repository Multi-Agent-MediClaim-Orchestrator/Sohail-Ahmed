"""Verification runs (03-03): start run, record step results, finalise, overrides, rerun planning.

The API is the system of record; n8n (or the inline orchestrator) only sequences. Deterministic checks are computed
HERE from the database — agent output can add explanations/warnings but never change a gating value."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from calc_engine import mapping as calc_mapping
from calc_engine.models import CalcInput
from claim_contract.enums import InsurerCaseStatus, Severity
from claim_contract.errors import ProblemError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..clients import calc as calc_client
from ..config.service import ConfigService
from ..db import Tx, sessionmaker, transaction
from ..ids import uuid7
from ..models.core import (
    CalculationResult,
    ClaimCase,
    Decision,
    FindingOverride,
    VerificationRun,
    VerificationStep,
)
from ..verification import engine
from ..verification.context import VCtx
from ..verification.outcome import StepOutcome, decide_run, step_status
from ..verification.schemas import (
    PREREQ,
    STEP_ORDER,
    Finding,
    RunStart,
    StepResultBody,
    evidence_ref,
    finding_key,
)
from . import assignment, audit, cases, context, events, jobs, outbox

# hooks registered by the query loop / decision gate; each: async fn(tx, case, run, decision_info)
NeedsInfoHook = Callable[[Tx, ClaimCase, VerificationRun, list[Finding]], Awaitable[None]]
ReadyHook = Callable[[Tx, ClaimCase, VerificationRun, Any], Awaitable[None]]
needs_info_hooks: list[NeedsInfoHook] = []
ready_hooks: list[ReadyHook] = []

log = logging.getLogger("verification")
TERMINAL_STEP = {"passed", "flagged", "failed", "skipped"}
PROCEDURE_GROUPS = {"surgeon_fees", "ot_charges", "anaesthesia", "implant", "procedure_package"}
NON_GATING = ("step.", "agent.")


def _json(model: Any) -> Any:
    return json.loads(model.model_dump_json())


def _fdump(findings: list[Finding]) -> list[dict[str, Any]]:
    return [json.loads(f.model_dump_json()) for f in findings]


def _fload(raw: Any) -> list[Finding]:
    return [Finding.model_validate(x) for x in (raw or [])]


# ================================================================================================== runs
async def start_run(cfg: ConfigService, case_id: UUID, body: RunStart, actor: str = "svc-n8n-insurer") -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        if case.status in ("closed", "settled", "approved", "partially_approved", "rejected"):
            raise ProblemError("case_closed", f"case is {case.status}", status=409)
        if body.client_token:
            same = (await s.execute(select(VerificationRun).where(VerificationRun.case_id == case_id, VerificationRun.trigger == body.trigger,
                                                                   VerificationRun.client_token == body.client_token))).scalar_one_or_none()
            if same is not None:
                return await _run_summary(s, same, existing=True)
        running = (await s.execute(select(VerificationRun).where(VerificationRun.case_id == case_id, VerificationRun.status == "running"))).scalars().all()
        if running and not body.force:
            if body.trigger == "query_response":
                case.pending_rerun = True  # follow-up run starts at finalize
            raise ProblemError("run_in_progress", "another verification run is in progress", status=409)
        for r in running:
            r.status, r.superseded, r.finished_at = "completed", True, clock.now()
        prev = (await s.execute(select(VerificationRun).where(VerificationRun.case_id == case_id).order_by(VerificationRun.run_no.desc()).limit(1))).scalar_one_or_none()
        vctx = await context.build_vctx(s, cfg, case)
        steps = _steps_to_run(body.steps, prev is not None)
        run = VerificationRun(id=uuid7(), case_id=case_id, run_no=(prev.run_no + 1) if prev else 1, trigger=body.trigger, status="running",
                              config_versions=vctx.config_versions, client_token=body.client_token)
        s.add(run)
        await s.flush()
        for st in STEP_ORDER:
            row = VerificationStep(id=uuid7(), run_id=run.id, step=st, status="pending", findings=[])
            if st not in steps and prev is not None:
                old = (await s.execute(select(VerificationStep).where(VerificationStep.run_id == prev.id, VerificationStep.step == st))).scalar_one_or_none()
                if old is not None and old.status in TERMINAL_STEP:  # carry forward the previous result
                    row.status, row.score, row.findings, row.agent_output, row.deterministic = old.status, old.score, old.findings, old.agent_output, old.deterministic
                    row.finished_at = old.finished_at
                    row.trace_id = "carried"
            elif st not in steps:
                row.status = "skipped"
            s.add(row)
        case.latest_run_id = run.id
        case.config_snapshot = vctx.config_versions
        t_auto = vctx.thresholds.t_auto_inr
        await assignment.assign_if_needed(s, case, Decimal(t_auto))
        if case.status in ("received", "needs_info"):
            await cases.transition(tx, case, InsurerCaseStatus.verifying, reason=f"run:{body.trigger}")
        elif case.status == "ready_for_decision" and body.trigger != "initial":
            await cases.transition(tx, case, InsurerCaseStatus.verifying, reason=f"run:{body.trigger}")
        await audit.append(s, case.id, "verification.run.started", actor_type="system", actor_id=actor,
                           payload={"run_id": str(run.id), "run_no": run.run_no, "trigger": body.trigger, "steps": steps}, config_versions=vctx.config_versions,
                           journey_id=case.journey_id)
        await s.flush()
        return await _run_summary(s, run, existing=False, steps_to_run=steps)


def _steps_to_run(requested: list[str], has_prev: bool) -> list[str]:
    if not requested or requested == ["all"] or not has_prev:
        return list(STEP_ORDER) if (requested == ["all"] or not has_prev) else []
    return [s for s in STEP_ORDER if s in requested]


async def _run_summary(s: AsyncSession, run: VerificationRun, *, existing: bool, steps_to_run: list[str] | None = None) -> dict[str, Any]:
    rows = (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run.id))).scalars().all()
    pending = [r.step for r in rows if r.status == "pending"]
    return {"run_id": str(run.id), "run_no": run.run_no, "status": run.status, "existing": existing, "config_versions": run.config_versions,
            "steps_to_run": steps_to_run if steps_to_run is not None else [st for st in STEP_ORDER if st in pending],
            "steps": [{"step": r.step, "status": r.status} for r in sorted(rows, key=lambda r: STEP_ORDER.index(r.step))]}


async def _get_run(s: AsyncSession, run_id: UUID, *, for_update: bool = False) -> VerificationRun:
    q = select(VerificationRun).where(VerificationRun.id == run_id)
    run = (await s.execute(q.with_for_update() if for_update else q)).scalar_one_or_none()
    if run is None:
        raise ProblemError("not_found", "run not found", status=404)
    if run.superseded:
        raise ProblemError("run_superseded", "a newer run replaced this one", status=409)
    return run


async def mark_step_running(run_id: UUID, step: str) -> dict[str, Any]:
    async with transaction() as tx:
        run = await _get_run(tx.session, run_id)
        row = await _step_row(tx.session, run.id, step)
        if row.status == "pending":
            row.status, row.started_at, row.attempt = "running", clock.now(), row.attempt + 1
        return {"attempt": row.attempt, "status": row.status}


async def _step_row(s: AsyncSession, run_id: UUID, step: str) -> VerificationStep:
    if step not in STEP_ORDER:
        raise ProblemError("validation_error", f"unknown step {step!r}", status=422)
    return (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run_id, VerificationStep.step == step).with_for_update())).scalar_one()


# ================================================================================================== step results
async def record_step(cfg: ConfigService, run_id: UUID, step: str, body: StepResultBody | None, actor: str = "svc-n8n-insurer") -> dict[str, Any]:
    """Compute (server-side) and persist one step. ``body`` carries agent output for agent-backed steps; None => rules only."""
    if body is not None and body.step.value != step:
        raise ProblemError("invalid_step_result", "step in body does not match the path", status=422)
    async with transaction() as tx:
        s = tx.session
        run = await _get_run(s, run_id)
        case = await cases.get_for_update(s, run.case_id)  # per-case serialisation (advisory-lock equivalent)
        rows = {r.step: r for r in (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run.id))).scalars().all()}
        row = rows[step]
        if row.status in TERMINAL_STEP and row.trace_id != "carried":
            sig = _body_sig(body)
            if (row.agent_output or {}).get("_sig") == sig or body is None:
                return await _after_step(s, run, rows, row, [], tx)  # idempotent repeat
            raise ProblemError("step_already_recorded", "a different result was already recorded for this step", status=409)
        if any(rows[p].status in ("pending", "running") for p in PREREQ.get(step, [])):
            raise ProblemError("step_out_of_order", f"{PREREQ[step]} must finish before {step}", status=409)
        vctx = await context.build_vctx(s, cfg, case)
        warnings: list[str] = []
        overrides = {r.finding_key for r in (await s.execute(select(FindingOverride).where(FindingOverride.case_id == case.id))).scalars()}
        calc_row_id: UUID | None = None
        if step == "document_fetch":
            out = await _document_fetch_outcome(s, case)
        elif step == "completeness":
            out = engine.check_completeness(vctx)
        elif step == "identity":
            out = engine.check_identity(vctx)
        elif step == "authenticity":
            out = engine.check_authenticity_rules(vctx)
        elif step == "coverage":
            out = engine.check_coverage(vctx)
        else:
            out, calc_row_id = await _calculation_outcome(s, case, run, vctx, body)
        findings = list(out.findings)
        if body is not None:
            findings += _agent_findings(body, out, warnings)
        for f in findings:
            if f.key is None:
                f.key = finding_key(step, f.code, evidence_ref(f))
            f.overridden = f.key in overrides
        status = step_status(findings, hard_fail=(out.status == "failed" and not any(f.severity is Severity.blocker for f in findings)))
        if out.status == "failed" and status != "failed":
            status = "failed"
        agent_out = dict(body.agent_output or {}) if body else {}
        if body is not None:
            agent_out.update({"agent": body.agent.model_dump() if body.agent else None, "notes": body.agent_notes, "degraded": body.degraded, "_sig": _body_sig(body)})
        row.status, row.score, row.findings = status, (Decimal(str(round(out.score, 3))) if out.score is not None else None), _fdump(findings)
        row.deterministic, row.agent_output = out.deterministic, agent_out or None
        row.trace_id = (body.agent.trace_id if body and body.agent else None)
        row.finished_at = clock.now()
        if body is not None and (body.degraded or body.failure):
            case.degraded = True
        await audit.append(
            s, case.id, "verification.step.completed", actor_type="agent" if (body and body.agent) else "system",
            actor_id=(f"{body.agent.name}@{body.agent.version}" if body and body.agent else "insurer-api"),
            payload={"step": step, "run_id": str(run.id), "status": status, "score": out.score, "findings": [f.code for f in findings], "warnings": warnings},
            config_versions=run.config_versions,
            model_info=({"prompt_version": body.agent.prompt_version, "trace_id": body.agent.trace_id} if body and body.agent else None), journey_id=case.journey_id,
        )
        result = await _after_step(s, run, rows, row, warnings, tx)
        cid, no = case.id, case.insurer_claim_no
        blockers = sum(1 for f in findings if f.severity is Severity.blocker and not f.overridden)
        tx.on_commit(lambda: events.publish("verification.step_completed", cid, {"step": step, "result": status, "blocker_count": blockers},
                                            ["reviewer", "senior_reviewer", "admin"], no))
        _ = calc_row_id
        return result


def _body_sig(body: StepResultBody | None) -> str | None:
    return None if body is None else hashlib.sha256(body.model_dump_json(exclude={"crew_attempts"}).encode()).hexdigest()


def _agent_findings(body: StepResultBody, out: StepOutcome, warnings: list[str]) -> list[Finding]:
    """Agents may add *warnings* only; server rules own blockers. A disagreement on a deterministic block is noted."""
    extra: list[Finding] = []
    for f in body.findings:
        if f.severity is Severity.blocker:
            f = f.model_copy(update={"severity": Severity.warning, "fixable": False})  # blockers come from rules above
        extra.append(f)
    if body.deterministic:
        for k, v in body.deterministic.items():
            mine = out.deterministic.get(k)
            if k in out.deterministic and v != mine and not (isinstance(v, float) and isinstance(mine, float) and abs(v - mine) <= 0.005):
                warnings.append("agent.disagrees_with_rules")
                extra.append(engine.make_finding("agent.disagrees_with_rules", body.step.value, detail=k))
                break
    if body.failure == "agent_unavailable":
        extra.append(engine.make_finding("step.agent_unavailable", body.step.value))
    elif body.failure == "agent_invalid_output":
        extra.append(engine.make_finding("step.agent_invalid_output", body.step.value))
    return extra


async def _document_fetch_outcome(s: AsyncSession, case: ClaimCase) -> StepOutcome:
    from .docs_fetch import docs_status

    st = await docs_status(s, case.id)
    findings: list[Finding] = []
    for f in st["failed"]:
        code = f["code"] if f["code"] in ("doc.hash_mismatch", "doc.virus_found", "doc.url_expired", "doc.fetch_failed") else "doc.fetch_failed"
        findings.append(engine.make_finding(code, "document_fetch", evidence=[engine.Evidence(doc_id=f["doc_id"])]))
    if st["pending"]:
        findings.append(engine.make_finding("doc.fetch_timeout", "document_fetch"))
    det = {"fetched": st["total"] - st["pending"] - len(st["failed"]), "failed": len(st["failed"]), "hash_mismatch": sum(f["status"] == "hash_mismatch" for f in st["failed"]),
           "infected": sum(f["code"] == "doc.virus_found" for f in st["failed"])}
    return StepOutcome(step_status(findings), None, findings, det)


# ------------------------------------------------------------------ calculation step
def build_calc_input(case: ClaimCase, lines: list[Any], vctx: VCtx, body: StepResultBody | None, sub: dict[str, Any]) -> tuple[CalcInput, int]:
    assert vctx.policy is not None and vctx.member is not None and vctx.rules is not None
    mapping = (body.line_mapping if body else None) or {}
    pol = vctx.policy
    grace_ok = pol.status == "lapsed" and pol.premium_paid_until and vctx.admitted_on <= pol.premium_paid_until + timedelta(days=pol.grace_days)
    status = "active" if (pol.status == "active" or grace_ok) else pol.status
    calc_lines: list[dict[str, Any]] = []
    agent_mapped = 0
    for ln in lines:
        ref = f"L{ln.line_no:04d}"
        m = mapping.get(ref) or mapping.get(str(ln.line_no))
        rule_map = calc_mapping.map_line(ln.category, ln.description)
        if m:
            group, tags, nm, imp, source, pg = m.get("mapped_group", "other"), m.get("tags", []), bool(m.get("is_non_medical")), bool(m.get("is_implant")), m.get("source", "agent"), m.get("procedure_group")
            agent_mapped += source == "agent"
        elif rule_map:
            group, tags, nm, imp, source, pg = rule_map.mapped_group.value, list(rule_map.tags), rule_map.is_non_medical, rule_map.is_implant, "rule", None
        else:
            group, tags, nm, imp, source, pg = "other", [], False, False, "rule", None
        if pg is None and group in PROCEDURE_GROUPS:
            pg = vctx.procedure_group
        days = int(ln.qty) if group in ("room_rent", "icu") and ln.qty == int(ln.qty) else None
        calc_lines.append({"line_ref": ref, "category": ln.category, "mapped_group": group, "procedure_group": pg, "description": ln.description[:200],
                           "qty": str(ln.qty), "unit_price": f"{ln.unit_price:.2f}", "claimed_amount": f"{ln.amount:.2f}",
                           "service_date": ln.service_date.isoformat() if ln.service_date else None, "days": days, "is_non_medical": nm,
                           "is_implant": imp, "exclusion_tags": tags, "mapping_source": source, "source_doc_id": str(ln.source_doc_id) if ln.source_doc_id else None,
                           "source_page": ln.source_page})
    payload = {
        "case_id": str(case.id), "claim_type": case.claim_type, "admission_type": case.admission_type,
        "policy": {"policy_number": vctx.patient.policy_number, "product_code": pol.product_code, "status": status, "start_date": pol.start_date.isoformat(),
                   "end_date": pol.end_date.isoformat(), "premium_paid_until": (pol.premium_paid_until or pol.end_date).isoformat(), "grace_days": pol.grace_days,
                   "sum_insured": f"{pol.sum_insured:.2f}", "bonus_sum": f"{pol.bonus:.2f}", "utilised_this_year": f"{vctx.utilised:.2f}",
                   "sum_insured_basis": vctx.rules.rules.sum_insured_basis},
        "member": {"member_id": vctx.patient.member_id, "relationship": "self", "dob": vctx.member.dob.isoformat(), "cover_start": vctx.member.cover_start.isoformat(),
                   "pre_existing": [{"icd_prefix": p} for p in vctx.member.pre_existing]},
        "admission": {"admitted_on": vctx.admitted_on.isoformat(), "discharged_on": vctx.discharged_on.isoformat(), "admission_type": case.admission_type,
                      "diagnosis_codes": vctx.diagnosis_codes, "procedure_codes": vctx.procedure_codes, "procedure_group": vctx.procedure_group, "day_care": vctx.admitted_on == vctx.discharged_on,
                      "hospital": {"hospital_id": vctx.hospital.code, "network_status": "network" if vctx.hospital.network_status == "network" else "non_network",
                                   "room_rent_tier": None}},
        "lines": calc_lines, "rules": json.loads(vctx.rules.rules.model_dump_json()), "rules_version": int(vctx.config_versions.get("policy_rules", 1)),
        "declared_total": f"{vctx.claimed_gross:.2f}",
    }
    return CalcInput.model_validate(payload), agent_mapped


async def _calculation_outcome(s: AsyncSession, case: ClaimCase, run: VerificationRun, vctx: VCtx, body: StepResultBody | None) -> tuple[StepOutcome, UUID | None]:
    from ..models.core import BillLine

    if vctx.policy is None or vctx.member is None or vctx.rules is None:
        return StepOutcome("skipped", None, [], {}), None
    lines = (await s.execute(select(BillLine).where(BillLine.case_id == case.id).order_by(BillLine.line_no))).scalars().all()
    sub = (await s.execute(select(ClaimCase.submission).where(ClaimCase.id == case.id))).scalar_one()
    inp, _ = build_calc_input(case, list(lines), vctx, body, sub)
    res = await calc_client.calculate(inp)
    row = CalculationResult(id=uuid7(), case_id=case.id, run_id=run.id, engine_version=res.engine_version, policy_rules_version=res.rules_version,
                            input=calc_client.input_dump(inp), output=json.loads(res.model_dump_json()), payable_amount=res.payable_total)
    s.add(row)
    await s.flush()
    findings: list[Finding] = []
    if res.payable_total < 0:
        findings.append(engine.make_finding("calc.negative_payable", "calculation"))
    unmapped = sum((Decimal(ln.claimed) for ln in res.lines if next(c for c in inp.lines if c.line_ref == ln.line_ref).mapped_group.value == "other"), Decimal("0"))
    if unmapped > res.claimed_total * Decimal("0.01"):
        findings.append(engine.make_finding("calc.unmapped_line", "calculation", detail=f"{unmapped}"))
    if res.blocked is not None:
        findings.append(engine.make_finding("calc.blocked", "calculation", detail=res.blocked.value))
    det = {"calc_result_id": str(row.id), "payable": f"{res.payable_total:.2f}", "claimed": f"{res.claimed_total:.2f}", "blocked": res.blocked.value if res.blocked else None,
           "flags": res.flag_codes(), "deductions": [{"line_ref": d.line_ref, "rule_id": d.rule_id, "amount": f"{d.amount.amount:.2f}"} for d in res.summary_deductions]}
    return StepOutcome(step_status(findings), None, findings, det), row.id


async def _after_step(s: AsyncSession, run: VerificationRun, rows: dict[str, VerificationStep], row: VerificationStep, warnings: list[str], tx: Tx) -> dict[str, Any]:
    """Mark dependants skipped when a prerequisite blocks them; compute the next step for n8n."""
    nxt: str | None = None
    for st in STEP_ORDER:
        r = rows[st]
        if r.status != "pending":
            continue
        blockers = [p for p in PREREQ.get(st, []) if rows[p].status in ("failed", "skipped") or (p == "completeness" and rows[p].status == "flagged")]
        if blockers:
            r.status, r.finished_at = "skipped", clock.now()
            r.findings = _fdump([engine.make_finding("step.skipped_due_to", st, severity="info", message=f"skipped: {blockers[0]} did not pass", detail=blockers[0])])
            continue
        nxt = st
        break
    return {"accepted": True, "step_status": row.status, "next_step": nxt or "finalize", "run_status": run.status, "warnings": warnings}


# ================================================================================================== finalize
async def finalize_run(cfg: ConfigService, run_id: UUID, actor: str = "svc-n8n-insurer") -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        run = (await s.execute(select(VerificationRun).where(VerificationRun.id == run_id).with_for_update())).scalar_one_or_none()
        if run is None:
            raise ProblemError("not_found", "run not found", status=404)
        if run.status == "completed" and run.outcome:  # idempotent
            return {"outcome": run.outcome, "case_status": (await s.get(ClaimCase, run.case_id)).status, "existing": True}  # type: ignore[union-attr]
        if run.superseded:
            raise ProblemError("run_superseded", "a newer run replaced this one", status=409)
        case = await cases.get_for_update(s, run.case_id)
        steps = (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run.id))).scalars().all()
        pending = [st.step for st in steps if st.status in ("pending", "running")]
        if pending:
            for st in steps:  # system could not finish: mark and send to manual verification
                if st.status in ("pending", "running"):
                    st.status, st.finished_at = "failed", clock.now()
                    st.findings = _fdump([engine.make_finding("step.agent_unavailable", st.step, message="step did not complete; manual verification required")])
        overrides = {r.finding_key for r in (await s.execute(select(FindingOverride).where(FindingOverride.case_id == case.id))).scalars()}
        all_findings: list[Finding] = []
        for st in sorted(steps, key=lambda r: STEP_ORDER.index(r.step)):
            for f in _fload(st.findings):
                f.overridden = f.overridden or (f.key in overrides)
                all_findings.append(f)
        manual = bool(pending) or any(f.code.startswith("step.agent_invalid_output") for f in all_findings) or case.degraded
        gating = [f for f in all_findings if not f.code.startswith(NON_GATING)]
        calc_row = (await s.execute(select(CalculationResult).where(CalculationResult.case_id == case.id).order_by(CalculationResult.created_at.desc()).limit(1))).scalar_one_or_none()
        calc_step = next((st for st in steps if st.step == "calculation"), None)
        have_calc = calc_row is not None and calc_step is not None and calc_step.status in ("passed", "flagged", "failed")
        det = calc_step.deterministic if (calc_step and calc_step.deterministic) else {}
        # The calculator works on bill lines (gross). A bill-level discount is the hospital's own concession: it comes off the
        # payable, so approved + deductions == claimed (contract V-12) and we never approve more than was claimed.
        calc_gross = Decimal(str(det.get("claimed"))) if have_calc and det.get("claimed") else None
        discount = max(Decimal("0.00"), calc_gross - case.claimed_amount) if calc_gross is not None else Decimal("0.00")
        payable_net = max(Decimal("0.00"), calc_row.payable_amount - discount) if have_calc and calc_row else None
        dec = decide_run(gating, calc_payable=payable_net,
                         calc_claimed=case.claimed_amount if calc_gross is not None else None,
                         calc_blocked=det.get("blocked") if have_calc else None, manual_verification=manual)
        # --- persist the recommendation (kind='recommendation') ---
        outcome_map = {"approve": "approve", "partial": "partial", "reject": "reject"}
        rec: Decision | None = None
        if dec.recommendation:
            amount = Decimal("0.00") if dec.recommendation == "reject" else (payable_net if payable_net is not None else Decimal("0.00"))
            deductions = (calc_row.output.get("summary_deductions") if calc_row else []) or []
            reason_codes = dec.reason_codes if dec.recommendation == "reject" else sorted({d["rule_id"] for d in deductions})
            rec = Decision(id=uuid7(), case_id=case.id, kind="recommendation", outcome=outcome_map[dec.recommendation], approved_amount=amount, reason_codes=reason_codes,
                           deductions=deductions, explanation=_explain(dec, amount, case), calc_result_id=calc_row.id if calc_row else None,
                           config_versions={**run.config_versions, "calc_engine_version": calc_row.engine_version if calc_row else None}, created_by="insurer-api", status="proposed",
                           flags=sorted({"degraded_mode"} if case.degraded else set()))
            s.add(rec)
            case.recommended_amount = amount
            await s.flush()
        run.status, run.finished_at, run.outcome = "completed", clock.now(), dec.next_status
        target = InsurerCaseStatus(dec.next_status)
        if case.status == "verifying":
            await cases.transition(tx, case, target, reason=f"run:{run.run_no}")
        await audit.append(s, case.id, "verification.run.completed", payload={"run_id": str(run.id), "outcome": dec.next_status, "recommendation": dec.recommendation or "none",
                                                                              "reason_codes": dec.reason_codes, "manual_verification": dec.manual_verification},
                           config_versions=run.config_versions, journey_id=case.journey_id)
        if rec is not None:
            await audit.append(s, case.id, "decision.recommended", actor_type="system", actor_id="insurer-api",
                               payload={"outcome": rec.outcome, "amount": str(rec.approved_amount), "calc_trace_id": str(rec.calc_result_id)}, config_versions=rec.config_versions)
        if dec.next_status == "needs_info":
            for hook in needs_info_hooks:
                await hook(tx, case, run, dec.fixable)
        elif case.status == "ready_for_decision":
            for rhook in ready_hooks:
                try:  # an auto-approval failure must never lose the verification result: savepoint + log
                    async with s.begin_nested():
                        await rhook(tx, case, run, dec)
                except Exception:
                    log.exception("ready_for_decision hook failed; case left for a human")
        if case.pending_rerun:
            case.pending_rerun = False
            tx.on_commit(lambda: jobs.enqueue("start_verification", case_id=str(case.id), trigger="query_response", steps=["completeness"]))
        cid, no, nxt = case.id, case.insurer_claim_no, dec.next_status
        tx.on_commit(lambda: events.publish("case.ready_for_decision" if nxt == "ready_for_decision" else "case.status_changed", cid,
                                            {"recommendation": dec.recommendation, "to": nxt}, ["reviewer", "senior_reviewer", "admin"], no))
        return {"outcome": dec.next_status, "recommendation": dec.recommendation, "reason_codes": dec.reason_codes, "manual_verification": dec.manual_verification,
                "case_status": case.status, "existing": False}


def _explain(dec: Any, amount: Decimal, case: ClaimCase) -> str:
    if dec.recommendation == "reject":
        return f"Rejection recommended: {', '.join(dec.reason_codes) or 'see findings'}. A human must confirm."
    if dec.recommendation == "partial":
        return f"Partial payment of {amount} recommended after policy rules (see deductions)."
    return f"Full payable amount {amount} recommended."


# ================================================================================================== overrides
async def override_finding(case_id: UUID, finding_key_: str, reason: str, principal: Any) -> dict[str, Any]:
    if len(reason.strip()) < 10:
        raise ProblemError("validation_error", "override reason must be at least 10 characters", status=422)
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        if case.status in ("closed", "settled", "approved", "partially_approved", "rejected"):
            raise ProblemError("case_closed", "case is final", status=409)
        run_id = case.latest_run_id
        if run_id is None:
            raise ProblemError("not_found", "no verification run yet", status=404)
        found: tuple[Finding, str] | None = None
        for st in (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run_id))).scalars():
            for f in _fload(st.findings):
                if f.key == finding_key_:
                    found = (f, st.step)
        if found is None:
            raise ProblemError("not_found", "finding not found on the latest run", status=404)
        f, step = found
        if step in ("identity", "authenticity") and f.severity is Severity.blocker and not principal.has_any(["senior_reviewer", "admin"]):
            raise ProblemError("forbidden_role", "overriding an identity/authenticity blocker requires senior_reviewer", status=403)
        ov = FindingOverride(id=uuid7(), case_id=case.id, run_id=run_id, finding_key=finding_key_, finding_code=f.code, reason=reason.strip(), overridden_by=principal.sub,
                             role=principal.primary_role)
        s.add(ov)
        await s.flush()
        await audit.append(s, case.id, "verification.override", actor_type="human", actor_id=principal.sub,
                           payload={"finding_code": f.code, "reason_hash": hashlib.sha256(reason.encode()).hexdigest(), "step": step, "severity": f.severity.value})
        return {"overridden": True, "finding_code": f.code, "step": step}


_ = (sessionmaker, outbox)
