"""Query loop service (03-05): drafts -> human approval -> hospital callback -> response -> triage -> re-verify; round 3 escalates."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import FieldError, ProblemError
from claim_contract.models import Query as ContractQuery
from claim_contract.models import QueryResponse as ContractResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..clients import crew
from ..config.schemas import QueryPolicy
from ..config.service import ConfigService
from ..db import Tx, sessionmaker, transaction
from ..ids import uuid7
from ..models.core import (
    CalculationResult,
    ClaimCase,
    ClaimDocument,
    Decision,
    Escalation,
    NetworkHospital,
    Query,
    QueryResponse,
    QueryRound,
    VerificationRun,
    VerificationStep,
)
from ..settings import get_settings
from ..verification import engine as veng
from ..verification.schemas import Finding
from . import audit, cases, events, jobs, orchestrator, outbox, query_logic, verification
from .query_logic import Triage

log = logging.getLogger("queries")
ALLOWED_DOCS = {d for d in ("discharge_summary", "final_bill", "itemised_bill", "pharmacy_bill", "lab_report", "radiology_report", "investigation_report", "admission_note",
                            "preauth_approval", "claim_form", "id_proof", "policy_card", "cancelled_cheque", "implant_sticker", "payment_receipt", "fir_mlc", "other")}
FINAL_STATES = ("approved", "partially_approved", "rejected", "settled", "closed")


def _cfg() -> ConfigService:
    return orchestrator._cfg()


async def _policy(s: AsyncSession) -> QueryPolicy:
    return (await _cfg().resolve(s, "query_policy", "default")).payload  # type: ignore[no-any-return]


async def _rounds(s: AsyncSession, case_id: UUID) -> list[QueryRound]:
    return list((await s.execute(select(QueryRound).where(QueryRound.case_id == case_id).order_by(QueryRound.round))).scalars())


async def _latest_findings(s: AsyncSession, case: ClaimCase) -> list[Finding]:
    if not case.latest_run_id:
        return []
    overrides = {r[0] for r in (await s.execute(text("SELECT finding_key FROM core.finding_override WHERE case_id = :c"), {"c": case.id})).all()}
    out: list[Finding] = []
    for st in (await s.execute(select(VerificationStep).where(VerificationStep.run_id == case.latest_run_id))).scalars():
        for f in verification._fload(st.findings):
            f.overridden = f.overridden or f.key in overrides
            out.append(f)
    return out


def contract_query(q: Query) -> ContractQuery:
    return ContractQuery.from_json_dict({
        "query_id": str(q.id), "round": q.round, "category": q.category, "text": q.text, "requested_doc_types": list(q.requested_doc_types or []),
        "due_by": q.due_by.isoformat(), "status": q.status if q.status != "draft_ready" else "open", "raised_by": q.raised_by or "agent:query-drafter"})


# ================================================================================================== drafting
async def create_draft(tx: Tx, case: ClaimCase, draft: query_logic.QueryDraft, pol: QueryPolicy, *, origin: str = "template", actor: str | None = None,
                       is_extension: bool = False) -> Query:
    s = tx.session
    hosp = await s.get(NetworkHospital, case.hospital_id)
    now = clock.now()
    due = query_logic.due_for_round(now, draft.round, pol)
    body = query_logic.render_template(draft.sections, hospital_name=hosp.name if hosp else "Hospital", claim_ref=case.hospital_claim_ref,
                                       insurer_claim_no=case.insurer_claim_no, rnd=draft.round, due_by=due)
    lint = query_logic.lint_query(body, requested_doc_types=draft.requested_doc_types, allowed_doc_types=ALLOWED_DOCS, finding_keys=draft.finding_keys,
                                  max_len=get_settings().query_text_max)
    q = Query(id=uuid7(), case_id=case.id, round=draft.round, category=draft.category, text=body, requested_doc_types=draft.requested_doc_types, status="draft_ready",
              origin=origin, draft_text=body, draft_citations=[{"type": "finding", "ref": k} for k in draft.finding_keys], raised_by=f"agent:query-drafter{('+human:' + actor) if actor else ''}",
              due_by=due, dedupe_key=draft.dedupe_key, finding_keys=draft.finding_keys, draft_source="template", lint_errors=[e.__dict__ for e in lint], is_extension=is_extension)
    s.add(q)
    await s.flush()
    await audit.append(s, case.id, "query.draft_generated", actor_type="agent" if not actor else "human", actor_id="query-drafter@template" if not actor else actor,
                       payload={"query_id": str(q.id), "round": q.round, "category": q.category, "source": "template", "lint_errors": [e.code for e in lint]},
                       journey_id=case.journey_id)
    cid, no, qid, rnd = case.id, case.insurer_claim_no, q.id, q.round
    tx.on_commit(lambda: events.publish("query.draft_ready", cid, {"query_id": str(qid), "round": rnd}, ["reviewer", "senior_reviewer", "admin"], no))
    if crew.enabled():
        tx.on_commit(lambda: jobs.enqueue("improve_query_draft", query_id=str(qid)))
    auto = (not lint and draft.round == 1 and draft.category in pol.auto_send_categories and q.draft_source == "template" and origin == "template" and not is_extension)
    if auto:
        q.auto_send = True
        await send_query(tx, case, q, actor="system:auto-send", auto=True)
    return q


async def on_needs_info(tx: Tx, case: ClaimCase, run: VerificationRun, fixable: list[Finding]) -> None:
    """Registered with ``verification.needs_info_hooks``; runs inside the finalize transaction."""
    s = tx.session
    pol = await _policy(s)
    rounds = await _rounds(s, case.id)
    last = rounds[-1] if rounds else None
    now = clock.now()
    if last is not None and last.closed_at is None:
        qs = (await s.execute(select(Query).where(Query.case_id == case.id, Query.round == last.round))).scalars().all()
        answered = [q for q in qs if q.status == "answered"]
        if not answered:
            return  # still waiting for the hospital; a rerun must not open another round (dedupe)
        for q in qs:
            if q.status in ("answered", "open", "draft_ready"):
                q.status, q.closed_reason = "closed", "reverified_unresolved" if q.status == "answered" else "superseded"
        last.closed_at, last.outcome = now, "partially_resolved"
        await audit.append(s, case.id, "round.closed", payload={"round": last.round, "outcome": "partially_resolved"})
        nxt = last.round + 1
    else:
        nxt = (last.round + 1) if last else 1
    if nxt > pol.max_rounds:
        await raise_escalation(tx, case, "unresolved_after_round_3", fixable)
        return
    docs_have = {r[0] for r in (await s.execute(text("SELECT doc_type FROM core.claim_document WHERE case_id = :c AND fetch_status = 'fetched' AND superseded_by IS NULL"), {"c": case.id})).all()}
    drafts = query_logic.build_queries(fixable, nxt, pol.max_queries_per_round, docs_have)
    for d in drafts:
        exists = (await s.execute(select(Query.id).where(Query.case_id == case.id, Query.dedupe_key == d.dedupe_key, Query.status.in_(("open", "draft_ready"))))).first()
        if exists:
            continue
        due = query_logic.due_for_round(now, nxt, pol)
        if (await s.get(QueryRound, (case.id, nxt))) is None:
            s.add(QueryRound(case_id=case.id, round=nxt, opened_at=now, due_by=due))
            await s.flush()
            await audit.append(s, case.id, "round.opened", payload={"round": nxt})
        await create_draft(tx, case, d, pol)


async def on_ready_for_decision(tx: Tx, case: ClaimCase, run: VerificationRun, dec: Any) -> None:
    """No fixable blockers remain: the open round is resolved and its queries are closed."""
    s = tx.session
    rounds = await _rounds(s, case.id)
    if not rounds or rounds[-1].closed_at is not None:
        return
    last = rounds[-1]
    for q in (await s.execute(select(Query).where(Query.case_id == case.id, Query.status.in_(("answered", "open", "draft_ready", "escalated"))))).scalars():
        q.status, q.closed_reason = "closed", "resolved" if q.status == "answered" else "no_longer_needed"
    last.closed_at, last.outcome = clock.now(), "resolved"
    await audit.append(s, case.id, "round.closed", payload={"round": last.round, "outcome": "resolved"})


verification.needs_info_hooks.append(on_needs_info)
verification.ready_hooks.insert(0, on_ready_for_decision)  # close the round before the gate (auto-approval) runs


# ================================================================================================== sending
def _assert_sendable(q: Query, pol: QueryPolicy) -> None:
    if q.status != "draft_ready":
        raise ProblemError("invalid_transition", f"query is {q.status}; only a draft_ready query can be sent")
    lint = [LE for LE in (q.lint_errors or [])]
    if lint:
        raise ProblemError("validation_error", "query has lint errors; fix the text or regenerate", errors=[FieldError(field="text", message=e.get("code", "lint")) for e in lint])
    if q.round > pol.max_rounds and not q.is_extension:
        raise ProblemError("invalid_transition", "round limit reached; a senior reviewer must resolve the escalation")


async def send_query(tx: Tx, case: ClaimCase, q: Query, *, actor: str, auto: bool = False) -> Query:
    s = tx.session
    pol = await _policy(s)
    _assert_sendable(q, pol)
    now = clock.now()
    q.due_by = query_logic.due_for_round(now, q.round, pol)
    q.status, q.sent_at = "open", now
    if not auto:
        q.raised_by = (q.raised_by or "agent:query-drafter").split("+human:")[0] + f"+human:{actor}"
    rnd = await s.get(QueryRound, (case.id, q.round))
    if rnd is None:
        s.add(QueryRound(case_id=case.id, round=q.round, opened_at=now, due_by=q.due_by))
    elif rnd.closed_at is None:
        rnd.due_by = q.due_by
    await s.flush()
    row = await outbox.enqueue_query(s, case, contract_query(q))
    q.callback_seq = row.seq
    await audit.append(s, case.id, "query.sent", actor_type="system" if auto else "human", actor_id=actor,
                       payload={"query_id": str(q.id), "round": q.round, "category": q.category, "auto": auto, "text_hash": hashlib.sha256(q.text.encode()).hexdigest()},
                       journey_id=case.journey_id)
    if case.status not in ("needs_info",):
        await cases.transition(tx, case, InsurerCaseStatus.needs_info, actor_type="human", actor_id=actor, reason="query_sent")
    cid, no, qid, r, due = case.id, case.insurer_claim_no, q.id, q.round, q.due_by
    tx.on_commit(lambda: events.publish("query.sent", cid, {"query_id": str(qid), "round": r, "due_by": due.isoformat()}, ["reviewer", "senior_reviewer", "admin"], no))
    return q


async def _get_query(s: AsyncSession, qid: UUID, *, lock: bool = False) -> tuple[Query, ClaimCase]:
    q = (await s.execute(select(Query).where(Query.id == qid).with_for_update() if lock else select(Query).where(Query.id == qid))).scalar_one_or_none()
    if q is None:
        raise ProblemError("unknown_query", "query not found", status=404)
    case = await cases.get_for_update(s, q.case_id)
    return q, case


def _etag(q: Query) -> str:
    return f'"q-{int(q.updated_at.timestamp() * 1000)}"'


async def edit_query(qid: UUID, user: Any, patch: dict[str, Any], if_match: str | None) -> dict[str, Any]:
    async with transaction() as tx:
        q, case = await _get_query(tx.session, qid, lock=True)
        if if_match and if_match != _etag(q):
            raise ProblemError("stale_etag", "query changed since you loaded it", status=412)
        if q.status != "draft_ready":
            raise ProblemError("invalid_transition", "only drafts can be edited")
        pol = await _policy(tx.session)
        if "text" in patch:
            q.text = patch["text"]
        if "requested_doc_types" in patch:
            q.requested_doc_types = list(patch["requested_doc_types"])
        if "due_by" in patch:
            hours = pol.round_sla_hours[q.round - 1]
            if patch["due_by"] > clock.now() + timedelta(hours=hours + 1):
                raise ProblemError("validation_error", f"due_by cannot exceed the round SLA ({hours} h)", status=422)
            q.due_by = patch["due_by"]
        hosp = await tx.session.get(NetworkHospital, case.hospital_id)
        lint = query_logic.lint_query(q.text, requested_doc_types=list(q.requested_doc_types), allowed_doc_types=ALLOWED_DOCS, finding_keys=list(q.finding_keys or []),
                                      max_len=get_settings().query_text_max, has_due_and_contact=True)
        q.lint_errors = [e.__dict__ for e in lint]
        await audit.append(tx.session, case.id, "query.edited", actor_type="human", actor_id=user.sub,
                           payload={"query_id": str(q.id), "query_edited": True, "fields": sorted(patch), "lint_errors": [e.code for e in lint],
                                    "diff_stats": {"chars": len(q.text), "draft_chars": len(q.draft_text or "")}})
        _ = hosp
        await tx.session.flush()
        return {"id": str(q.id), "status": q.status, "lint_errors": q.lint_errors, "etag": _etag(q)}


async def send_by_id(qid: UUID, user: Any) -> dict[str, Any]:
    async with transaction() as tx:
        q, case = await _get_query(tx.session, qid, lock=True)
        await send_query(tx, case, q, actor=user.sub)
        return {"id": str(q.id), "status": q.status, "sent_at": q.sent_at.isoformat() if q.sent_at else None, "due_by": q.due_by.isoformat()}


async def close_query(qid: UUID, user: Any, reason: str) -> dict[str, Any]:
    if len(reason.strip()) < 3:
        raise ProblemError("validation_error", "a reason is required", status=422)
    async with transaction() as tx:
        q, case = await _get_query(tx.session, qid, lock=True)
        if q.status == "closed":
            return {"id": str(q.id), "status": "closed"}
        q.status, q.closed_reason = "closed", reason.strip()
        await audit.append(tx.session, case.id, "query.closed", actor_type="human", actor_id=user.sub, payload={"query_id": str(q.id), "reason_hash": hashlib.sha256(reason.encode()).hexdigest()})
        return {"id": str(q.id), "status": "closed"}


async def human_query(case_id: UUID, user: Any, category: str, body_text: str, doc_types: list[str], send: bool = False) -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        if case.status in FINAL_STATES:
            raise ProblemError("case_closed", f"case is {case.status}", status=409)
        pol = await _policy(s)
        rounds = await _rounds(s, case_id)
        rnd = rounds[-1].round if rounds and rounds[-1].closed_at is None else (rounds[-1].round + 1 if rounds else 1)
        if rnd > pol.max_rounds:
            raise ProblemError("invalid_transition", "round limit reached", status=409)
        if (await s.get(QueryRound, (case_id, rnd))) is None:
            s.add(QueryRound(case_id=case_id, round=rnd, opened_at=clock.now(), due_by=query_logic.due_for_round(clock.now(), rnd, pol)))
            await s.flush()
        due0 = query_logic.due_for_round(clock.now(), rnd, pol)
        body_text = query_logic.ensure_footer(body_text, due0, case.insurer_claim_no)
        lint = query_logic.lint_query(body_text, requested_doc_types=doc_types, allowed_doc_types=ALLOWED_DOCS, finding_keys=[], max_len=get_settings().query_text_max)
        q = Query(id=uuid7(), case_id=case_id, round=rnd, category=category, text=body_text, requested_doc_types=doc_types, status="draft_ready", origin="human", raised_by=f"human:{user.sub}",
                  due_by=query_logic.due_for_round(clock.now(), rnd, pol), draft_source=None, lint_errors=[e.__dict__ for e in lint],
                  dedupe_key=hashlib.sha256(f"human|{body_text}|{rnd}".encode()).hexdigest())
        s.add(q)
        await s.flush()
        await audit.append(s, case_id, "query.draft_generated", actor_type="human", actor_id=user.sub, payload={"query_id": str(q.id), "round": rnd, "source": "human"})
        if send:
            await send_query(tx, case, q, actor=user.sub)
        return {"id": str(q.id), "status": q.status, "round": rnd, "lint_errors": q.lint_errors}


async def redraft(case_id: UUID, user: Any) -> dict[str, Any]:
    """POST /cases/{id}/queries/draft: rebuild drafts from the open fixable findings of the latest run."""
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        if case.status in FINAL_STATES:
            raise ProblemError("case_closed", f"case is {case.status}", status=409)
        pol = await _policy(s)
        findings = await _latest_findings(s, case)
        rounds = await _rounds(s, case_id)
        rnd = rounds[-1].round if rounds and rounds[-1].closed_at is None else (rounds[-1].round + 1 if rounds else 1)
        if rnd > pol.max_rounds:
            raise ProblemError("invalid_transition", "round limit reached; resolve the escalation instead", status=409)
        # an unanswered earlier round is closed as unanswered when the reviewer forces the next one
        if rounds and rounds[-1].closed_at is not None and rounds[-1].outcome == "unanswered":
            for q in (await s.execute(select(Query).where(Query.case_id == case_id, Query.status == "open"))).scalars():
                q.status, q.closed_reason = "closed", "superseded"
        docs_have = {r[0] for r in (await s.execute(text("SELECT doc_type FROM core.claim_document WHERE case_id = :c AND fetch_status = 'fetched' AND superseded_by IS NULL"), {"c": case_id})).all()}
        drafts = query_logic.build_queries(findings, rnd, pol.max_queries_per_round, docs_have)
        if (await s.get(QueryRound, (case_id, rnd))) is None and drafts:
            s.add(QueryRound(case_id=case_id, round=rnd, opened_at=clock.now(), due_by=query_logic.due_for_round(clock.now(), rnd, pol)))
            await s.flush()
        out = []
        for d in drafts:
            if (await s.execute(select(Query.id).where(Query.case_id == case_id, Query.dedupe_key == d.dedupe_key, Query.status.in_(("open", "draft_ready"))))).first():
                continue
            q = await create_draft(tx, case, d, pol, origin="human", actor=user.sub)
            out.append(str(q.id))
        return {"drafts": out, "round": rnd}


# ================================================================================================== hospital response
async def receive_response(hospital: NetworkHospital, query_id: UUID, resp: ContractResponse, idem_key: UUID) -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        q = (await s.execute(select(Query).where(Query.id == query_id).with_for_update())).scalar_one_or_none()
        case = await s.get(ClaimCase, q.case_id) if q else None
        if q is None or case is None or case.hospital_id != hospital.id or q.sent_at is None:
            raise ProblemError("unknown_query", "query not found", status=404)
        case = await cases.get_for_update(s, case.id)
        replay = (await s.execute(select(QueryResponse).where(QueryResponse.query_id == q.id, QueryResponse.idempotency_key == idem_key))).scalar_one_or_none()
        if replay is not None:
            return {"status": "received", "query_status": q.status}
        if q.status == "closed":
            raise ProblemError("query_closed", "this query is closed", status=409)
        late = q.status == "escalated"
        amends: UUID | None = None
        if q.status == "answered":
            prev = (await s.execute(select(QueryResponse).where(QueryResponse.query_id == q.id).order_by(QueryResponse.received_at.desc()))).scalars().first()
            if prev is None or prev.triage is not None:
                raise ProblemError("idempotency_conflict", "this query was already answered and triaged", status=409)
            amends = prev.id
        if resp.attached_doc_ids:
            have = {r[0] for r in (await s.execute(select(ClaimDocument.id).where(ClaimDocument.case_id == case.id, ClaimDocument.id.in_(resp.attached_doc_ids)))).all()}
            missing = [str(d) for d in resp.attached_doc_ids if d not in have]
            if missing:
                raise ProblemError("missing_attachments", "attached documents must be supplied first via the documents endpoint", status=422,
                                   errors=[FieldError(field="attached_doc_ids", message=m) for m in missing])
        requested_missing = set(q.requested_doc_types or [])
        if requested_missing and not resp.answer_text.strip() and not resp.attached_doc_ids:
            raise ProblemError("validation_error", "empty response", status=422)
        s.add(QueryResponse(id=uuid7(), query_id=q.id, answer_text=resp.answer_text, attached_doc_ids=list(resp.attached_doc_ids), responded_by=resp.responded_by, idempotency_key=idem_key, amends=amends))
        if not late:
            q.status = "answered"
        q.answered_at = clock.now()
        q.response = resp.model_dump(mode="json")
        case.last_inbound_seq += 1
        await s.flush()
        await audit.append(s, case.id, "query.response_received", actor_type="external", actor_id=f"hospital:{hospital.hospital_code}",
                           payload={"query_id": str(q.id), "round": q.round, "docs": len(resp.attached_doc_ids), "late": late, "amendment": amends is not None,
                                    "answer_hash": hashlib.sha256(resp.answer_text.encode()).hexdigest()}, journey_id=case.journey_id)
        cid, no, qid, rnd = case.id, case.insurer_claim_no, q.id, q.round
        tx.on_commit(lambda: events.publish("query.answered", cid, {"query_id": str(qid), "round": rnd, "late": late}, ["reviewer", "senior_reviewer", "admin"], no))
        if late:
            tx.on_commit(lambda: events.publish("escalation.raised", cid, {"reason": "late_response"}, ["senior_reviewer", "admin"], no))
        else:
            tx.on_commit(lambda: jobs.enqueue("triage_response", query_id=str(qid)))
        return {"status": "received", "query_status": "escalated" if late else "answered"}


# ================================================================================================== triage
@jobs.register("triage_response")
async def triage_response(query_id: str) -> dict[str, Any]:
    qid = UUID(query_id)
    async with sessionmaker()() as s:
        q = await s.get(Query, qid)
        if q is None:
            return {"skipped": True}
        resp = (await s.execute(select(QueryResponse).where(QueryResponse.query_id == qid).order_by(QueryResponse.received_at.desc()))).scalars().first()
        if resp is None or resp.triage is not None:
            return {"skipped": True}
        case_id = q.case_id
        attached = list(resp.attached_doc_ids or [])
    await orchestrator._ensure_documents(case_id)
    async with sessionmaker()() as s:
        q = await s.get(Query, qid)
        resp = (await s.execute(select(QueryResponse).where(QueryResponse.query_id == qid).order_by(QueryResponse.received_at.desc()))).scalars().first()
        assert q is not None and resp is not None
        docs = (await s.execute(select(ClaimDocument).where(ClaimDocument.id.in_(attached)))).scalars().all() if attached else []
        attached_types = {d.doc_type for d in docs if d.fetch_status == "fetched"}
        findings = {f.key: f for f in await _latest_findings(s, await s.get(ClaimCase, case_id))}  # type: ignore[arg-type]
        for_doc: dict[str, list[str]] = {}
        for f in findings.values():
            for d in f.suggested_doc_types:
                for_doc.setdefault(d.value, []).append(f.key or "")
        qkeys, rtypes, answer = list(q.finding_keys or []), list(q.requested_doc_types or []), resp.answer_text
        doc_summaries = [{"doc_id": str(d.id), "doc_type": d.doc_type, "pages": d.pages, "parse_confidence": float(d.parse_confidence or 0), "extract_masked": d.extract or {}, "text_excerpt_masked": ""} for d in docs]
    triage = query_logic.rules_triage(query_finding_keys=qkeys, requested_doc_types=rtypes, attached_doc_types=attached_types, answer_text=answer, attached_count=len(attached))
    if crew.enabled():
        try:
            out = await crew.call(crew.PATH_QUERY_TRIAGE, case_id, {
                "open_findings": [{"key": k, "kind": findings[k].code, "severity": "blocker"} for k in qkeys if k in findings], "requested_doc_types": rtypes,
                "response_text_masked": answer, "attached_docs": doc_summaries,
                "code_check": {"requested_present": sorted(set(rtypes) & attached_types), "requested_missing": sorted(set(rtypes) - attached_types), "unexpected": sorted(attached_types - set(rtypes))}})
            verdict_map = {"resolved": "sufficient", "partially_resolved": "partial", "unresolved": "insufficient", "off_topic": "off_topic"}
            agent = Triage(verdict_map.get(out.get("verdict", ""), "partial"), list(out.get("resolved_finding_keys", [])), list(out.get("remaining_finding_keys", [])), out.get("notes", ""), "llm")
            triage = query_logic.apply_triage(agent, query_finding_keys=qkeys, requested_doc_types=rtypes, attached_doc_types=attached_types, answer_text=answer, attached_count=len(attached), finding_keys_for_doc=for_doc)
        except (crew.CrewUnavailable, crew.CrewInvalidOutput):
            log.info("triage fell back to rules")
    return await _apply_triage_result(qid, triage, attached_types)


async def _apply_triage_result(qid: UUID, triage: Triage, attached_types: set[str]) -> dict[str, Any]:
    async with transaction() as tx:
        s = tx.session
        q, case = await _get_query(s, qid, lock=True)
        resp = (await s.execute(select(QueryResponse).where(QueryResponse.query_id == qid).order_by(QueryResponse.received_at.desc()))).scalars().first()
        assert resp is not None
        tj = {"verdict": triage.verdict, "resolved_findings": triage.resolved_finding_keys, "remaining": triage.remaining_finding_keys, "notes": triage.notes[:400],
              "agent": {"source": triage.source}}
        resp.triage, resp.triage_source = tj, triage.source if triage.source in ("llm", "rules") else "rules"
        q.triage = tj
        await audit.append(s, case.id, "query.triaged", actor_type="agent" if triage.source == "llm" else "system", actor_id="triage@" + triage.source,
                           payload={"query_id": str(q.id), "verdict": triage.verdict, "remaining": len(triage.remaining_finding_keys)})
        cid, no = case.id, case.insurer_claim_no
        verdict = triage.verdict
        if verdict in ("sufficient", "partial") and case.status in ("needs_info", "escalated"):
            steps = veng.plan_rerun(sorted(attached_types)) or ["document_fetch", "completeness"]
            steps = [x for x in steps if x != "document_fetch"] or ["completeness"]
            tx.on_commit(lambda: jobs.enqueue("start_verification", case_id=str(cid), trigger="query_response", steps=steps))
        else:
            tx.on_commit(lambda: events.publish("query.answered", cid, {"query_id": str(qid), "verdict": verdict, "needs_reviewer": True}, ["reviewer", "senior_reviewer", "admin"], no))
        return {"verdict": verdict}


async def override_triage(qid: UUID, user: Any, verdict: str, note: str) -> dict[str, Any]:
    if verdict not in ("sufficient", "partial", "insufficient", "off_topic") or len(note.strip()) < 5:
        raise ProblemError("validation_error", "valid verdict and a note are required", status=422)
    async with transaction() as tx:
        q, case = await _get_query(tx.session, qid, lock=True)
        resp = (await tx.session.execute(select(QueryResponse).where(QueryResponse.query_id == qid).order_by(QueryResponse.received_at.desc()))).scalars().first()
        if resp is None:
            raise ProblemError("invalid_transition", "no response to triage", status=409)
        tj = {**(resp.triage or {}), "verdict": verdict, "notes": note.strip()[:400], "overridden_by": user.sub}
        resp.triage, resp.triage_source, q.triage = tj, "reviewer_override", tj
        await audit.append(tx.session, case.id, "query.triaged", actor_type="human", actor_id=user.sub, payload={"query_id": str(q.id), "verdict": verdict, "override": True})
        cid = case.id
        if verdict in ("sufficient", "partial") and case.status == "needs_info":
            tx.on_commit(lambda: jobs.enqueue("start_verification", case_id=str(cid), trigger="query_response", steps=["completeness"]))
        return {"verdict": verdict}


# ================================================================================================== timers
async def reminder(qid: UUID) -> dict[str, Any]:
    async with transaction() as tx:
        q, case = await _get_query(tx.session, qid, lock=True)
        if q.status != "open":
            return {"skipped": True}
        q.reminder_count += 1
        cid, no, n = case.id, case.insurer_claim_no, q.reminder_count
        tx.on_commit(lambda: events.publish("query.overdue", cid, {"query_id": str(qid), "reminder": n}, ["reviewer", "senior_reviewer", "admin"], no))
        return {"reminder_count": q.reminder_count}


async def on_timeout(case_id: UUID, round_no: int) -> dict[str, Any]:
    """Round deadline passed. Unanswered rounds 1-2 raise an overdue alert (no automatic next round); round 3 escalates."""
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        rnd = await s.get(QueryRound, (case_id, round_no))
        if rnd is None or rnd.closed_at is not None:
            return {"ignored": True}  # a timer for an already-closed round (clock skew / late n8n) is ignored
        pol = await _policy(s)
        answered = (await s.execute(select(Query.id).where(Query.case_id == case_id, Query.round == round_no, Query.status == "answered"))).first()
        if answered:
            return {"ignored": True}
        rnd.closed_at, rnd.outcome = clock.now(), "unanswered"
        await audit.append(s, case.id, "round.closed", payload={"round": round_no, "outcome": "unanswered"})
        cid, no = case.id, case.insurer_claim_no
        if round_no >= pol.max_rounds:
            await raise_escalation(tx, case, "no_response_round_3", [])
            return {"escalated": True}
        tx.on_commit(lambda: events.publish("query.overdue", cid, {"round": round_no, "unanswered": True}, ["reviewer", "senior_reviewer", "admin"], no))
        return {"overdue": True}


async def due_items(within_minutes: int = 10) -> list[dict[str, Any]]:
    now = clock.now()
    horizon = now + timedelta(minutes=within_minutes)
    out: list[dict[str, Any]] = []
    async with sessionmaker()() as s:
        pol = await _policy(s)
        rows = (await s.execute(select(Query).where(Query.status == "open", Query.sent_at.is_not(None)))).scalars().all()
        for q in rows:
            rnd = await s.get(QueryRound, (q.case_id, q.round))
            if rnd is not None and rnd.closed_at is not None:
                continue
            if q.due_by <= horizon:
                out.append({"type": "round_timeout", "case_id": str(q.case_id), "query_id": str(q.id), "round": q.round})
                continue
            offs = sorted(pol.reminder_offsets_hours, reverse=True)
            if q.reminder_count < len(offs) and q.due_by - timedelta(hours=offs[q.reminder_count]) <= horizon:
                out.append({"type": "reminder", "query_id": str(q.id), "case_id": str(q.case_id)})
    return out


# ================================================================================================== escalation
def build_pack(case: ClaimCase, reason: str, rounds: list[QueryRound], queries: list[Query], findings: list[Finding], calc_payable: str | None) -> dict[str, Any]:
    timeline = [{"ts": q.sent_at.isoformat(), "event": "query.sent", "round": q.round} for q in queries if q.sent_at] + \
               [{"ts": q.answered_at.isoformat(), "event": "query.answered", "round": q.round} for q in queries if q.answered_at]
    timeline.sort(key=lambda x: str(x["ts"]))
    unresolved = [{"key": f.key, "code": f.code, "evidence": [e.model_dump(exclude_none=True) for e in f.evidence][:3], "attempts": len(rounds)} for f in findings
                  if f.severity.value == "blocker" and f.fixable and not f.overridden]
    contradictory = len({q.round for q in queries if q.triage and q.triage.get("verdict") == "off_topic"}) > 0
    return {"case_id": str(case.id), "reason": reason, "timeline": timeline,
            "rounds": [{"round": r.round, "outcome": r.outcome, "query_ids": [str(q.id) for q in queries if q.round == r.round]} for r in rounds],
            "unresolved_findings": unresolved, "responses": [{"query_id": str(q.id), "triage": (q.triage or {}).get("verdict")} for q in queries if q.answered_at],
            "calc_preview": {"payable_if_decided_now": calc_payable}, "suggested_actions": ["decide_now", "request_more", "reject_for_noncompliance"],
            "risk_flags": ["contradictory_responses"] if contradictory else []}


async def raise_escalation(tx: Tx, case: ClaimCase, reason: str, findings: list[Finding]) -> Escalation:
    s = tx.session
    existing = (await s.execute(select(Escalation).where(Escalation.case_id == case.id, Escalation.status == "open"))).scalar_one_or_none()
    if existing is not None:
        return existing
    rounds = await _rounds(s, case.id)
    queries = list((await s.execute(select(Query).where(Query.case_id == case.id).order_by(Query.round, Query.created_at))).scalars())
    calc = (await s.execute(select(CalculationResult).where(CalculationResult.case_id == case.id).order_by(CalculationResult.created_at.desc()).limit(1))).scalar_one_or_none()
    for r in rounds:
        if r.closed_at is None:
            r.closed_at, r.outcome = clock.now(), "escalated"
    for q in queries:
        if q.status in ("open", "draft_ready"):
            q.status = "escalated"
    pack = build_pack(case, reason, rounds, queries, findings or await _latest_findings(s, case), f"{calc.payable_amount:.2f}" if calc else None)
    esc = Escalation(id=uuid7(), case_id=case.id, reason=reason, status="open", pack=pack)
    s.add(esc)
    if case.status != "escalated":
        await cases.transition(tx, case, InsurerCaseStatus.escalated, reason=reason)
    await audit.append(s, case.id, "escalation.raised", payload={"reason": reason, "round": rounds[-1].round if rounds else 0})
    cid, no = case.id, case.insurer_claim_no
    tx.on_commit(lambda: events.publish("escalation.raised", cid, {"reason": reason}, ["senior_reviewer", "admin"], no))
    await s.flush()
    return esc


async def resolve_escalation(case_id: UUID, user: Any, action: str, note: str, query_body: dict[str, Any] | None) -> dict[str, Any]:
    if action not in ("request_more", "decide_now", "reject_for_noncompliance") or len(note.strip()) < 5:
        raise ProblemError("validation_error", "valid action and a note are required", status=422)
    async with transaction() as tx:
        s = tx.session
        case = await cases.get_for_update(s, case_id)
        esc = (await s.execute(select(Escalation).where(Escalation.case_id == case_id, Escalation.status == "open").with_for_update())).scalar_one_or_none()
        if esc is None:
            raise ProblemError("invalid_transition", "no open escalation", status=409)
        pol = await _policy(s)
        out: dict[str, Any] = {"action": action}
        if action == "request_more":
            used = (await s.execute(select(Query).where(Query.case_id == case_id, Query.is_extension.is_(True)))).scalars().all()
            if len(used) >= pol.allowed_extension:
                raise ProblemError("invalid_transition", "the single extension query was already used", status=409)
            body = query_body or {}
            if len(body.get("text", "")) < 10:
                raise ProblemError("validation_error", "query.text required for request_more", status=422)
            rnd = pol.max_rounds
            body["text"] = query_logic.ensure_footer(body["text"], query_logic.due_for_round(clock.now(), rnd, pol), case.insurer_claim_no)
            lint = query_logic.lint_query(body["text"], requested_doc_types=list(body.get("requested_doc_types", [])), allowed_doc_types=ALLOWED_DOCS, finding_keys=[], max_len=get_settings().query_text_max)
            q = Query(id=uuid7(), case_id=case_id, round=rnd, category=body.get("category", "other"), text=body["text"], requested_doc_types=list(body.get("requested_doc_types", [])),
                      status="draft_ready", origin="human", raised_by=f"human:{user.sub}", due_by=query_logic.due_for_round(clock.now(), rnd, pol), is_extension=True,
                      lint_errors=[e.__dict__ for e in lint], dedupe_key=hashlib.sha256(f"ext|{body['text']}".encode()).hexdigest())
            s.add(q)
            await s.flush()
            s.add(QueryRound(case_id=case_id, round=rnd, opened_at=clock.now(), due_by=q.due_by)) if (await s.get(QueryRound, (case_id, rnd))) is None else None
            r3 = await s.get(QueryRound, (case_id, rnd))
            if r3 is not None:
                r3.closed_at, r3.outcome, r3.due_by = None, None, q.due_by  # reopen round 3 for the single extension
            await s.flush()
            await cases.transition(tx, case, InsurerCaseStatus.needs_info, actor_type="human", actor_id=user.sub, reason="escalation_request_more")
            await send_query(tx, case, q, actor=user.sub)
            out["query_id"] = str(q.id)
        elif action == "decide_now":
            await cases.transition(tx, case, InsurerCaseStatus.ready_for_decision, actor_type="human", actor_id=user.sub, reason="escalation_decide_now", note="decided with unresolved findings")
        else:
            rec = (await s.execute(select(Decision).where(Decision.case_id == case_id, Decision.kind == "recommendation").order_by(Decision.created_at.desc()).limit(1))).scalar_one_or_none()
            s.add(Decision(id=uuid7(), case_id=case_id, kind="recommendation", outcome="reject", approved_amount=0, reason_codes=["DOCS_NOT_PROVIDED"], deductions=[],
                           explanation="Rejection for non-compliance: requested documents were not provided after the maximum number of query rounds.",
                           calc_result_id=rec.calc_result_id if rec else None, config_versions=rec.config_versions if rec else {}, created_by=user.sub, status="proposed", flags=["escalated_case"]))
            await cases.transition(tx, case, InsurerCaseStatus.ready_for_decision, actor_type="human", actor_id=user.sub, reason="escalation_reject_for_noncompliance")
        esc.status, esc.resolved_at, esc.resolved_by, esc.action, esc.note = "resolved", clock.now(), user.sub, action, note.strip()
        await audit.append(s, case_id, "escalation.resolved", actor_type="human", actor_id=user.sub, payload={"action": action, "note_hash": hashlib.sha256(note.encode()).hexdigest()})
        return out


async def improve_draft_with_crew(query_id: str) -> dict[str, Any]:
    """Upgrade a template draft with the crew's text when it passes lint (regen limit 3, then keep the template)."""
    qid = UUID(query_id)
    async with sessionmaker()() as s:
        q = await s.get(Query, qid)
        if q is None or q.status != "draft_ready" or q.draft_source != "template":
            return {"skipped": True}
        case = await s.get(ClaimCase, q.case_id)
        findings = [f for f in await _latest_findings(s, case) if f.key in set(q.finding_keys or [])]  # type: ignore[arg-type]
        hosp = await s.get(NetworkHospital, case.hospital_id)  # type: ignore[union-attr]
        rnd, case_id = q.round, q.case_id
        ctx = {"round": rnd, "hospital_name": hosp.name if hosp else "Hospital", "tone": "firm" if rnd >= 3 else "standard", "prior_queries": [],
               "findings": [{"key": f.key, "kind": f.code, "severity": f.severity.value, "detail": f.message, "requested_doc_type": (f.suggested_doc_types[0].value if f.suggested_doc_types else None)} for f in findings],
               "requirements": [{"doc_type": d, "rule": "required by policy document list"} for d in (q.requested_doc_types or [])]}
    for attempt in range(get_settings().query_regen_max):
        try:
            out = await crew.call(crew.PATH_QUERY_DRAFT, case_id, ctx)
        except (crew.CrewUnavailable, crew.CrewInvalidOutput):
            return {"fallback": "template"}
        lint = query_logic.lint_query(out["text"], requested_doc_types=list(out.get("requested_doc_types", [])), allowed_doc_types=ALLOWED_DOCS, finding_keys=[], max_len=get_settings().query_text_max,
                                      has_due_and_contact=False)
        async with transaction() as tx:
            q = await tx.session.get(Query, qid)
            if q is None or q.status != "draft_ready":
                return {"skipped": True}
            q.regen_count = attempt + 1
            if not lint:
                q.text, q.draft_text, q.draft_source, q.origin = out["text"], out["text"], "llm", "agent_draft"
                q.draft_citations = [{"type": "clause", "ref": c.get("chunk_id"), "snippet": c.get("quote")} for c in out.get("citations", [])] or q.draft_citations
                q.lint_errors = []
                await tx.session.flush()
                return {"upgraded": True}
            q.lint_errors = [e.__dict__ for e in lint]
    return {"fallback": "template_after_regen"}


jobs.register("improve_query_draft")(improve_draft_with_crew)
_ = (ContractResponse,)
