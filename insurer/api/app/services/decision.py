"""Decision gate service (03-04): recommendation -> gate -> (auto | reviewer | approver tasks) -> final decision."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import FieldError, ProblemError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..config.service import ConfigService
from ..db import Tx, transaction
from ..ids import uuid7
from ..models.core import (
    Approval,
    CalculationResult,
    ClaimCase,
    Decision,
    DecisionTask,
    Escalation,
    NetworkHospital,
    Policy,
    VerificationRun,
    VerificationStep,
)
from ..settings import get_settings
from ..verification.schemas import Finding
from . import (
    approval_rules,
    audit,
    cases,
    decision_builder,
    events,
    flags,
    jobs,
    outbox,
    utilisation,
    verification,
)
from .approval_rules import ApprovalError, Vote
from .gate import AutoFacts, Thresholds, compute_gate, explain_gate

CENT = Decimal("0.01")
escalation_hook: Callable[[UUID, str], Awaitable[None]] | None = None  # set by the query loop (05)


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str
    approved_amount: Decimal | None = None
    reason_codes: list[str] = Field(default_factory=list)
    deduction_edits: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None
    override_reason: str | None = None


class VoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    comment: str | None = None


def _q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


# ================================================================================================== gate inputs
async def _latest_rec(s: AsyncSession, case_id: UUID) -> Decision | None:
    return (await s.execute(select(Decision).where(Decision.case_id == case_id, Decision.kind == "recommendation").order_by(Decision.created_at.desc()).limit(1))).scalar_one_or_none()


async def _gate_context(s: AsyncSession, cfg: ConfigService, case: ClaimCase) -> dict[str, Any]:
    th_res = await cfg.resolve(s, "thresholds", "default")
    thp = th_res.payload
    st = get_settings()
    th = Thresholds(thp.t_auto_inr, thp.t_four_inr, allow_auto=thp.auto_approval_enabled and st.allow_auto_approval,
                    allow_reviewer_final=st.allow_reviewer_tier_final, auto_min_identity=thp.auto_min_identity_score)
    run = await s.get(VerificationRun, case.latest_run_id) if case.latest_run_id else None
    findings: list[Finding] = []
    steps: dict[str, VerificationStep] = {}
    overrides = {r[0] for r in (await s.execute(text("SELECT finding_key FROM core.finding_override WHERE case_id = :c"), {"c": case.id})).all()}
    if run is not None:
        for stp in (await s.execute(select(VerificationStep).where(VerificationStep.run_id == run.id))).scalars():
            steps[stp.step] = stp
            for f in verification._fload(stp.findings):
                f.overridden = f.overridden or (f.key in overrides)
                findings.append(f)
    return {"th": th, "thp": thp, "th_version": th_res.version, "run": run, "findings": findings, "steps": steps}


def _auto_facts(outcome: str, ctx: dict[str, Any], case: ClaimCase) -> AutoFacts:
    active = [f for f in ctx["findings"] if not f.overridden and f.severity.value in ("blocker", "warning")]
    ident = ctx["steps"].get("identity")
    return AutoFacts(outcome=outcome, active_blockers=sum(f.severity.value == "blocker" for f in active), active_warnings=sum(f.severity.value == "warning" for f in active),
                     identity_score=float(ident.score) if ident and ident.score is not None else None,
                     manual_verification=any(f.code.startswith("step.agent_invalid_output") for f in ctx["findings"]), degraded=case.degraded)


async def _flag_inputs(s: AsyncSession, case: ClaimCase, ctx: dict[str, Any], rec: Decision, approved: Decimal) -> flags.FlagInputs:
    hosp = await s.get(NetworkHospital, case.hospital_id)
    pol = await s.get(Policy, case.policy_id) if case.policy_id else None
    util = Decimal("0")
    total_si = Decimal("0")
    if pol is not None:
        total_si = pol.sum_insured + pol.cumulative_bonus
        sub_adm = (await s.execute(text("SELECT submission #>> '{admission,admitted_on}' FROM core.claim_case WHERE id = :i"), {"i": case.id})).scalar_one()
        from datetime import date

        util = await utilisation.utilised(s, pol.id, utilisation.policy_year(pol.start_date, date.fromisoformat(sub_adm)))
    esc = (await s.execute(select(Escalation).where(Escalation.case_id == case.id, Escalation.action == "decide_now"))).scalar_one_or_none() is not None
    payable = rec.approved_amount or Decimal("0")
    return flags.FlagInputs(findings=ctx["findings"], degraded=case.degraded, manual_verification=any(f.code.startswith("step.agent_invalid_output") for f in ctx["findings"]),
                            watchlist_hospital=bool(hosp and hosp.watchlist), utilised=util, payable=payable, sum_insured_total=total_si, approved_amount=approved, escalated=esc)


async def preview(cfg: ConfigService, case_id: UUID) -> dict[str, Any]:
    """GET /decision: current recommendation, gate preview with explanation, open task and votes."""
    from ..db import sessionmaker

    async with sessionmaker()() as s:
        case = await s.get(ClaimCase, case_id)
        if case is None:
            raise ProblemError("unknown_claim", "case not found", status=404)
        rec = await _latest_rec(s, case_id)
        out: dict[str, Any] = {"case_status": case.status, "etag": case.etag, "recommendation": None, "gate": None, "task": None, "votes": []}
        if rec is not None:
            ctx = await _gate_context(s, cfg, case)
            fl = flags.collect_review_flags(await _flag_inputs(s, case, ctx, rec, rec.approved_amount or Decimal("0")), ctx["thp"].review_flags, ctx["thp"].high_utilisation_pct)
            g = compute_gate(case.claimed_amount, rec.approved_amount or Decimal("0"), rec.outcome, ctx["th"], fl, _auto_facts(rec.outcome, ctx, case))
            out["recommendation"] = {"id": str(rec.id), "outcome": rec.outcome, "approved_amount": f"{rec.approved_amount:.2f}", "reason_codes": list(rec.reason_codes or []),
                                     "deductions": rec.deductions, "explanation": rec.explanation, "flags": list(rec.flags or [])}
            out["gate"] = {"tier": g.tier, "required_approvals": g.required, "gate_amount": f"{g.gate_amount:.2f}", "min_senior": g.min_senior, "flags": list(g.flags),
                           "thresholds_v": ctx["th_version"], "explanation": explain_gate(g, ctx["th"]), "reasons": list(g.reasons)}
        task = (await s.execute(select(DecisionTask).where(DecisionTask.case_id == case_id, DecisionTask.status == "open"))).scalar_one_or_none()
        if task is not None:
            votes = (await s.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
            out["task"] = {"id": str(task.id), "decision_id": str(task.decision_id), "tier": task.tier, "required_approvals": task.required_approvals, "min_senior": task.min_senior,
                           "allowed_roles": task.allowed_roles}
            out["votes"] = [{"role": v.approver_role, "verdict": v.verdict, "valid": v.valid} for v in votes]  # names are not exposed to non-admins
        return out


# ================================================================================================== submit
def _validate_submit(body: SubmitBody, rec: Decision, calc: CalculationResult | None, case: ClaimCase, reject_reasons: list[str], user: Any) -> tuple[Decimal, list[dict[str, Any]]]:
    if body.outcome not in ("approve", "partial", "reject"):
        raise ProblemError("validation_error", "outcome must be approve, partial or reject (needs_info is raised by the query loop)", status=422)
    payable = calc.payable_amount if calc is not None else Decimal("0")
    deductions = [dict(x) for x in (rec.deductions or [])]
    if body.outcome == "reject":
        codes = [c for c in body.reason_codes if c in reject_reasons or c.startswith(("auth.", "coverage.", "calc.", "identity.", "completeness."))]
        if not codes:
            raise ProblemError("validation_error", "reject needs at least one reason code from policy_rules.reject_reasons", status=422,
                               errors=[FieldError(field="reason_codes", message=f"allowed: {reject_reasons}")])
        if body.approved_amount not in (None, Decimal("0"), Decimal("0.00")):
            raise ProblemError("validation_error", "approved_amount must be absent or 0.00 for a rejection", status=422)
        return Decimal("0.00"), []
    if body.approved_amount is None or body.approved_amount <= 0:
        raise ProblemError("validation_error", "approved_amount must be > 0 (use reject for a zero payable)", status=422)
    amt = _q(body.approved_amount)
    if amt != body.approved_amount.quantize(CENT):
        raise ProblemError("validation_error", "approved_amount must have at most 2 decimals", status=422)
    senior = user.has_any(["senior_reviewer", "admin"])
    if amt > payable:
        if not (senior and (body.override_reason or "").strip()):
            raise ProblemError("exceeds_calculation", f"approved_amount {amt} exceeds engine payable {payable}; senior_reviewer with override_reason required", status=422,
                               errors=[FieldError(field="approved_amount", message=f"max {payable} for role reviewer")])
    if amt > case.claimed_amount:
        raise ProblemError("amount_not_reconciled", f"approved_amount {amt} exceeds the claimed amount {case.claimed_amount}", status=422)
    if amt < payable and not body.reason_codes:
        raise ProblemError("validation_error", "every manual decrease needs a reason code", status=422)
    manual = Decimal("0")
    for i, e in enumerate(body.deduction_edits):
        try:
            a = Decimal(str(e["amount"]))
        except Exception as exc:
            raise ProblemError("validation_error", f"deduction_edits[{i}].amount invalid", status=422) from exc
        if a <= 0 or a != a.quantize(CENT):
            raise ProblemError("validation_error", f"deduction_edits[{i}].amount must be a positive 2dp decimal", status=422)
        manual += a
        deductions.append({"line_ref": e.get("line_ref", "manual"), "rule_id": e.get("rule_id", "manual"), "amount": {"amount": f"{a:.2f}", "currency": "INR"},
                           "explanation": str(e.get("explanation", ""))[:500]})
    total_ded = sum((Decimal(str(d["amount"]["amount"] if isinstance(d["amount"], dict) else d["amount"])) for d in deductions), Decimal("0"))
    # approved + all deductions must reconcile with the claimed amount (±0.01). Manual edits explain the reviewer's decrease.
    gap = abs(amt + total_ded - case.claimed_amount)
    if gap > CENT and amt <= payable:
        # reviewer lowered the amount without itemising: account for the difference as one manual deduction (must carry a reason code)
        diff = (payable - amt) - manual
        if diff > 0:
            deductions.append({"line_ref": "manual", "rule_id": "manual", "amount": {"amount": f"{diff:.2f}", "currency": "INR"}, "explanation": (body.note or "Manual adjustment by reviewer")[:500]})
            total_ded += diff
            gap = abs(amt + total_ded - case.claimed_amount)
    if gap > CENT and amt <= payable:
        raise ProblemError("amount_not_reconciled", f"approved {amt} + deductions {total_ded} != claimed {case.claimed_amount}", status=422)
    return amt, deductions


async def submit_decision(cfg: ConfigService, case_id: UUID, user: Any, body: SubmitBody, if_match: str | None) -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        cases.check_etag(case, if_match)
        if case.status != "ready_for_decision":
            raise ProblemError("invalid_transition", f"case is {case.status}; a decision can only be submitted from ready_for_decision")
        rec = await _latest_rec(s, case_id)
        if rec is None:
            raise ProblemError("invalid_transition", "no recommendation exists for this case yet")
        calc = await s.get(CalculationResult, rec.calc_result_id) if rec.calc_result_id else None
        ctx = await _gate_context(s, cfg, case)
        pr = await _policy_reject_reasons(s, cfg, case)
        amt, deductions = _validate_submit(body, rec, calc, case, pr, user)
        fl = flags.collect_review_flags(await _flag_inputs(s, case, ctx, rec, amt), ctx["thp"].review_flags, ctx["thp"].high_utilisation_pct)
        # a human is submitting: auto tier does not apply (facts=None); the reviewer tier is the lowest
        gate = compute_gate(case.claimed_amount, amt, body.outcome, ctx["th"], fl, None)
        stamp = decision_builder.version_stamp({**(rec.config_versions or {}), "thresholds": ctx["th_version"]}, (rec.config_versions or {}).get("calc_engine_version"))
        dec = Decision(id=uuid7(), case_id=case.id, kind="final" if gate.tier == "reviewer" else "pending_approval", outcome=body.outcome, approved_amount=amt,
                       reason_codes=list(body.reason_codes), deductions=deductions, explanation=rec.explanation, calc_result_id=rec.calc_result_id, gate_tier=gate.tier,
                       config_versions=stamp, created_by=user.sub, status="awaiting_approval", gate_amount=gate.gate_amount, note=body.note,
                       override_reason=body.override_reason, flags=list(gate.flags), supersedes=None)
        s.add(dec)
        await s.flush()
        await audit.append(s, case.id, "decision.submitted", actor_type="human", actor_id=user.sub, payload={"decision_id": str(dec.id), "outcome": dec.outcome, "amount": str(amt),
                                                                                                             "tier": gate.tier, "flags": list(gate.flags)}, config_versions=stamp)
        gate_json = {"tier": gate.tier, "required_approvals": gate.required, "gate_amount": f"{gate.gate_amount:.2f}", "flags": list(gate.flags), "thresholds_v": ctx["th_version"],
                     "explanation": explain_gate(gate, ctx["th"])}
        if gate.tier == "reviewer":
            res = await finalize(tx, case, dec, approver_ids=[], actor=user.sub)
            return {"decision_id": str(dec.id), "kind": "final", "gate": gate_json, "case_status": case.status, "task_id": None, **res}
        task = DecisionTask(id=uuid7(), case_id=case.id, decision_id=dec.id, tier=gate.tier, required_approvals=gate.required, min_senior=gate.min_senior,
                            allowed_roles=list(gate.roles), status="open", opened_by=user.sub,
                            threshold_snapshot={"t_auto": str(ctx["th"].t_auto), "t_four": str(ctx["th"].t_four), "thresholds_v": ctx["th_version"], "gate_amount": str(gate.gate_amount), "flags": list(gate.flags)})
        s.add(task)
        await cases.transition(tx, case, InsurerCaseStatus.awaiting_approval, actor_type="human", actor_id=user.sub, reason="decision_submitted")
        cid, no, tid = case.id, case.insurer_claim_no, task.id
        payload = {"tier": gate.tier, "task_id": str(tid), "gate_amount": str(gate.gate_amount)}
        tx.on_commit(lambda: events.publish("approval.requested", cid, payload, ["approver", "senior_reviewer", "admin"], no))
        tx.on_commit(lambda: events.publish("decision.recommended", cid, {"outcome": body.outcome, "gate_tier": gate.tier}, ["reviewer", "senior_reviewer", "admin"], no))
        return {"decision_id": str(dec.id), "kind": "pending_approval", "gate": gate_json, "case_status": "awaiting_approval", "task_id": str(tid)}


async def _policy_reject_reasons(s: AsyncSession, cfg: ConfigService, case: ClaimCase) -> list[str]:
    from ..models.core import InsuranceProduct

    if not case.policy_id:
        return ["POLICY_INACTIVE"]
    pol = await s.get(Policy, case.policy_id)
    prod = await s.get(InsuranceProduct, pol.product_id)  # type: ignore[union-attr]
    try:
        return list((await cfg.resolve(s, "policy_rules", prod.code)).payload.reject_reasons)  # type: ignore[union-attr]
    except Exception:
        return []


# ================================================================================================== votes
async def record_vote(cfg: ConfigService, decision_id: UUID, user: Any, body: VoteBody) -> dict[str, Any]:
    st = get_settings()
    async with transaction() as tx:
        s = tx.session
        dec = (await s.execute(select(Decision).where(Decision.id == decision_id).with_for_update())).scalar_one_or_none()
        if dec is None:
            raise ProblemError("not_found", "decision not found", status=404)
        case = await cases.get_for_update(s, dec.case_id)
        task = (await s.execute(select(DecisionTask).where(DecisionTask.decision_id == decision_id).order_by(DecisionTask.opened_at.desc()).limit(1).with_for_update())).scalar_one_or_none()
        if task is None or task.status != "open":
            raise ProblemError("task_closed", "no open approval task for this decision", status=409)
        votes = (await s.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
        prior = [Vote(v.approver, v.verdict, frozenset([v.approver_role]), v.valid) for v in votes]
        try:
            approval_rules.check_vote(user=user.sub, user_roles=user.roles, allowed_roles=frozenset(task.allowed_roles), verdict=body.verdict, comment=body.comment, prior_votes=prior,
                                      submitter=dec.created_by, assignee=case.assigned_reviewer, sod=st.segregation_of_duties)
        except ApprovalError as exc:
            raise ProblemError(exc.code, exc.detail, status=exc.status) from exc
        s.add(Approval(id=uuid7(), decision_id=dec.id, task_id=task.id, approver=user.sub, verdict=body.verdict, comment=body.comment, approver_role=user.primary_role))
        await s.flush()
        await audit.append(s, case.id, "approval.voted", actor_type="human", actor_id=user.sub,
                           payload={"decision_id": str(dec.id), "verdict": body.verdict, "role": user.primary_role, "comment_hash": hashlib.sha256((body.comment or "").encode()).hexdigest()})
        cid, no = case.id, case.insurer_claim_no
        if body.verdict in ("return", "reject"):
            await s.execute(text("UPDATE core.approval SET valid = false WHERE task_id = :t"), {"t": task.id})
            task.status, task.closed_at, task.close_reason = "returned", clock.now(), body.verdict
            dec.status = "returned"
            await audit.append(s, case.id, "decision.returned", actor_type="human", actor_id=user.sub, payload={"decision_id": str(dec.id), "verdict": body.verdict})
            await cases.transition(tx, case, InsurerCaseStatus.ready_for_decision, actor_type="human", actor_id=user.sub, reason=f"approval_{body.verdict}")
            tx.on_commit(lambda: events.publish("approval.voted", cid, {"verdict": body.verdict, "remaining": 0}, ["reviewer", "approver", "senior_reviewer", "admin"], no))
            return {"status": "returned"}
        votes = (await s.execute(select(Approval).where(Approval.task_id == task.id, Approval.valid.is_(True), Approval.verdict == "approve"))).scalars().all()
        ok, remaining, senior_needed = approval_rules.can_finalize([Vote(v.approver, v.verdict, frozenset([v.approver_role]) | (frozenset({"senior_reviewer"}) if v.approver_role == "senior_reviewer" else frozenset())) for v in votes],
                                                                  task.required_approvals, task.min_senior)
        if ok:
            res = await finalize(tx, case, dec, approver_ids=[v.approver for v in votes], actor=user.sub, task=task)
            return {"status": "final", "outcome": dec.outcome, "case_status": case.status, **res}
        tx.on_commit(lambda: events.publish("approval.voted", cid, {"verdict": "approve", "remaining": remaining, "senior_needed": senior_needed}, ["reviewer", "approver", "senior_reviewer", "admin"], no))
        return {"status": "pending", "remaining": remaining, "senior_needed": senior_needed}


# ================================================================================================== finalize
FINAL_STATUS = {"approve": InsurerCaseStatus.approved, "partial": InsurerCaseStatus.partially_approved, "reject": InsurerCaseStatus.rejected}


async def finalize(tx: Tx, case: ClaimCase, dec: Decision, *, approver_ids: list[str], actor: str, task: DecisionTask | None = None, auto: bool = False) -> dict[str, Any]:
    s = tx.session
    now = clock.now()
    reviewers = ["system:auto-approval"] if auto else list(dict.fromkeys([dec.created_by, *approver_ids]))
    dec.kind, dec.status, dec.finalised_at, dec.reviewer_ids = "final", "finalised", now, reviewers
    if dec.outcome in ("approve", "partial"):
        pol = await s.get(Policy, case.policy_id) if case.policy_id else None
        if pol is not None:
            from datetime import date

            adm = (await s.execute(text("SELECT submission #>> '{admission,admitted_on}' FROM core.claim_case WHERE id = :i"), {"i": case.id})).scalar_one()
            await utilisation.apply(s, pol.id, utilisation.policy_year(pol.start_date, date.fromisoformat(adm)), dec.approved_amount or Decimal("0"))
    if task is not None:
        task.status, task.closed_at, task.close_reason = "completed", now, "finalised"
    target = FINAL_STATUS[dec.outcome]
    await cases.transition(tx, case, target, actor_type="system" if auto else "human", actor_id=actor, reason="decision_final", notify=False,
                           approved_amount=dec.approved_amount if dec.outcome != "reject" else Decimal("0.00"))
    cd = decision_builder.to_contract(dec)
    await outbox.enqueue_decision(s, case, cd)
    await outbox.enqueue_status(s, case, decision=None)
    await audit.append(s, case.id, "decision.finalised", actor_type="system" if auto else "human", actor_id=actor,
                       payload={"decision_id": str(dec.id), "outcome": dec.outcome, "amount": str(dec.approved_amount), "tier": dec.gate_tier or "", "auto": auto, "reviewers": reviewers},
                       config_versions=dec.config_versions)
    if not auto:
        etype = "human.approved" if dec.outcome != "reject" else "human.rejected"
        await audit.append(s, case.id, etype, actor_type="human", actor_id=actor, payload={"decision_id": str(dec.id), "role": "approver" if approver_ids else "reviewer",
                                                                                         "reason_codes": list(dec.reason_codes or []), "second_approver": approver_ids[1] if len(approver_ids) > 1 else None})
    cid, no, amount, outcome = case.id, case.insurer_claim_no, dec.approved_amount, dec.outcome
    tx.on_commit(lambda: events.publish("decision.final", cid, {"outcome": outcome, "approved_amount": str(amount), "auto": auto}, ["reviewer", "approver", "senior_reviewer", "admin"], no))
    if dec.outcome != "reject" and (dec.approved_amount or 0) > 0:
        tx.on_commit(lambda: jobs.enqueue("initiate_settlement", case_id=str(cid)))
    return {"outcome": dec.outcome, "approved_amount": f"{dec.approved_amount:.2f}", "auto": auto}


# ================================================================================================== auto approval hook
async def on_ready_for_decision(tx: Tx, case: ClaimCase, run: VerificationRun, dec_info: Any) -> None:
    """Runs inside ``finalize_run``'s transaction. Clean claims within T_auto are approved without a human (user decision)."""
    s = tx.session
    if dec_info.recommendation not in ("approve", "partial"):
        return
    rec = await _latest_rec(s, case.id)
    if rec is None:
        return
    from ..config.service import (
        ConfigService as _CS,  # config service lives on the app; reuse the orchestrator's instance
    )
    from . import orchestrator

    cfg = orchestrator._cfg() if orchestrator.cfg is not None else _CS()
    ctx = await _gate_context(s, cfg, case)
    fl = flags.collect_review_flags(await _flag_inputs(s, case, ctx, rec, rec.approved_amount or Decimal("0")), ctx["thp"].review_flags, ctx["thp"].high_utilisation_pct)
    gate = compute_gate(case.claimed_amount, rec.approved_amount or Decimal("0"), rec.outcome, ctx["th"], fl, _auto_facts(rec.outcome, ctx, case))
    rec.gate_tier, rec.gate_amount, rec.flags = gate.tier, gate.gate_amount, list(gate.flags)
    if gate.tier != "auto":
        return
    final = Decision(id=uuid7(), case_id=case.id, kind="final", outcome=rec.outcome, approved_amount=rec.approved_amount, reason_codes=list(rec.reason_codes or []),
                     deductions=list(rec.deductions or []), explanation=rec.explanation, calc_result_id=rec.calc_result_id, gate_tier="auto",
                     config_versions=decision_builder.version_stamp({**(rec.config_versions or {}), "thresholds": ctx["th_version"]}, (rec.config_versions or {}).get("calc_engine_version")),
                     created_by="system:auto-approval", status="finalised", gate_amount=gate.gate_amount, note="auto-approved: clean claim within T_auto", flags=[])
    s.add(final)
    await s.flush()
    try:
        async with s.begin_nested():
            await finalize(tx, case, final, approver_ids=[], actor="system:auto-approval", auto=True)
    except utilisation.SumInsuredExhausted:
        final.status = "withdrawn"  # leave the case for a human; the savepoint rolled back the partial finalisation
        await s.flush()


verification.ready_hooks.append(on_ready_for_decision)


# ================================================================================================== queue / withdraw / stats
async def approval_queue(user: Any) -> list[dict[str, Any]]:
    from ..db import sessionmaker

    async with sessionmaker()() as s:
        rows = (await s.execute(text(
            "SELECT t.id, t.decision_id, t.case_id, t.tier, t.required_approvals, t.min_senior, t.allowed_roles, t.opened_at, t.threshold_snapshot, c.insurer_claim_no, "
            "c.claimed_amount, c.assigned_reviewer, d.created_by, d.outcome, d.approved_amount, "
            "(SELECT count(*) FROM core.approval a WHERE a.task_id = t.id AND a.valid AND a.verdict = 'approve') AS approvals, "
            "EXISTS (SELECT 1 FROM core.approval a WHERE a.task_id = t.id AND a.approver = :u) AS i_voted "
            "FROM core.decision_task t JOIN core.claim_case c ON c.id = t.case_id JOIN core.decision d ON d.id = t.decision_id WHERE t.status = 'open' ORDER BY t.opened_at"), {"u": user.sub})).all()
    out = []
    st = get_settings()
    for r in rows:
        if not (user.roles & set(r.allowed_roles)) or r.i_voted:
            continue
        if st.segregation_of_duties and user.sub in (r.created_by, r.assigned_reviewer):
            continue
        out.append({"task_id": str(r.id), "decision_id": str(r.decision_id), "case_id": str(r.case_id), "insurer_claim_no": r.insurer_claim_no, "tier": r.tier,
                    "required_approvals": r.required_approvals, "approvals_so_far": r.approvals, "min_senior": r.min_senior, "claimed_amount": f"{r.claimed_amount:.2f}",
                    "recommended": {"outcome": r.outcome, "approved_amount": f"{r.approved_amount:.2f}"}, "opened_at": r.opened_at.isoformat(), "allowed_roles": r.allowed_roles})
    return out


async def withdraw_task(case_id: UUID, user: Any, reason: str) -> dict[str, Any]:
    if len(reason.strip()) < 5:
        raise ProblemError("validation_error", "a reason is required", status=422)
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        task = (await s.execute(select(DecisionTask).where(DecisionTask.case_id == case_id, DecisionTask.status == "open").with_for_update())).scalar_one_or_none()
        if task is None:
            raise ProblemError("task_closed", "no open task", status=409)
        task.status, task.closed_at, task.close_reason = "cancelled", clock.now(), reason
        await s.execute(text("UPDATE core.decision SET status = 'withdrawn' WHERE id = :d"), {"d": task.decision_id})
        await s.execute(text("UPDATE core.approval SET valid = false WHERE task_id = :t"), {"t": task.id})
        await cases.transition(tx, case, InsurerCaseStatus.ready_for_decision, actor_type="human", actor_id=user.sub, reason="task_withdrawn")
        await audit.append(s, case.id, "decision.returned", actor_type="human", actor_id=user.sub, payload={"decision_id": str(task.decision_id), "verdict": "withdrawn"})
        return {"status": "cancelled"}


async def gate_stats() -> dict[str, Any]:
    from ..db import sessionmaker

    async with sessionmaker()() as s:
        rows = (await s.execute(text("SELECT tier, status, tasks, avg_seconds FROM core.v_gate_stats"))).all()
    return {"tiers": [{"tier": r.tier, "status": r.status, "tasks": r.tasks, "avg_seconds": float(r.avg_seconds or 0)} for r in rows]}
