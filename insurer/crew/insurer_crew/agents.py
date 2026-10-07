"""The seven agents (03-08 §6.5). Each agent = deterministic precompute (facts) + one or more schema-constrained LLM calls +
code ``enforce`` that overrides anything the model got wrong. No agent can write anywhere: they only *return* values."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import tools, validators
from .runtime import PromptRegistry, Settings, Trace
from .schemas import (
    ALL_CORES,
    ALL_OUTPUTS,
    SEV_RANK,
    AuthenticityContext,
    AuthenticityCore,
    CalcMapContext,
    CalcMapCore,
    Citation,
    ClauseHit,
    CoverageContext,
    CoverageCore,
    DocType,
    IdentityContext,
    IdentityCore,
    IdentityIssue,
    QueryDraftContext,
    QueryDraftCore,
    SupervisorContext,
    SupervisorCore,
    TriageContext,
    TriageCore,
)

TEMPLATE = Path(__file__).parent / "templates" / "query_skeleton.md"
REPAIR_TMPL = "Your previous reply was not valid. Return ONLY corrected JSON that satisfies the schema. Problems: {errors}"


class AgentInvalidOutput(Exception):
    def __init__(self, errors: Any = None) -> None:
        super().__init__("agent_invalid_output")
        self.errors = errors


class ContextInvalid(Exception):
    pass


class PiiInContext(Exception):
    def __init__(self, hits: dict[str, set[str]]) -> None:
        super().__init__("pii_in_context")
        self.fields = sorted(hits)  # field paths only, never the matched text


@dataclass
class Deps:
    llm: Any
    settings: Settings
    prompts: PromptRegistry
    rag: Any = None


@dataclass
class Runner:
    """Per-request helper handed to an agent: tracked LLM calls with JSON repair."""

    agent: str
    deps: Deps
    trace: Trace
    alias: str
    max_tokens: int = 1024
    timeout: float = 60
    usage_prompt: int = 0
    usage_completion: int = 0
    degraded: bool = False
    served_alias: str = ""
    warnings: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)

    def system_prompt(self, name: str) -> str:
        text, ver = self.deps.prompts.get(name)
        if ver not in self.prompt_versions:
            self.prompt_versions.append(ver)
        return text

    async def ask(self, core: type[BaseModel], system_name: str, user: str) -> BaseModel:
        """One schema-constrained call; up to ``crew_repair_retries`` repair attempts, then ``AgentInvalidOutput``."""
        messages = [{"role": "system", "content": self.system_prompt(system_name)}, {"role": "user", "content": user}]
        schema = core.model_json_schema()
        retries = self.deps.settings.crew_repair_retries
        for attempt in range(retries + 1):
            async with self.trace.span("llm", agent=self.agent, attempt=attempt):
                res = await self.deps.llm.complete(alias=self.alias, messages=messages, schema=schema, metadata={"agent": self.agent, "prompt_version": self.prompt_versions[-1], "claim_ref": self.trace.case_id,
                                                                                                                 "request_id": self.trace.request_id, "trace_id": self.trace.id},
                                                   max_tokens=self.max_tokens, timeout=self.timeout)
            self.usage_prompt += res.prompt_tokens
            self.usage_completion += res.completion_tokens
            self.degraded = self.degraded or res.degraded
            self.served_alias = res.served_by or self.alias
            try:
                return core.model_validate(validators.parse_json(res.text))
            except (ValidationError, ValueError) as e:
                errs = e.errors() if isinstance(e, ValidationError) else [{"msg": str(e)}]
                if attempt == retries:
                    raise AgentInvalidOutput(errs) from e
                self.trace.repairs += 1
                compact = "; ".join(f"{'.'.join(str(p) for p in x.get('loc', ()))}: {x.get('msg')}" for x in errs)[:400]
                messages = messages + [{"role": "assistant", "content": res.text[:2000]}, {"role": "user", "content": REPAIR_TMPL.format(errors=compact)}]
        raise AgentInvalidOutput()  # pragma: no cover


def _docs_block(docs: list[Any], limit_tokens: int) -> tuple[str, bool]:
    """Wrap document summaries as untrusted data; truncate oldest low-priority docs beyond the context budget."""
    out, used, truncated = [], 0, False
    for d in docs:
        dd = d.model_dump() if isinstance(d, BaseModel) else dict(d)
        piece = json.dumps({k: dd.get(k) for k in ("doc_id", "doc_type", "pages", "parse_confidence", "extract_masked", "text_excerpt_masked") if k in dd}, ensure_ascii=False)
        n = len(piece) // 4
        if used + n > limit_tokens:
            truncated = True
            continue
        used += n
        out.append(validators.wrap_untrusted(str(dd.get("doc_id", "doc")), piece))
    return "\n".join(out), truncated


def _base_dict(m: BaseModel) -> dict[str, Any]:
    return m.model_dump(mode="json")


# ================================================================================================ identity
async def identity(ctx: IdentityContext, r: Runner) -> dict[str, Any]:
    member = ctx.member or {}
    patient = ctx.patient or {}
    member_name = member.get("full_name") or member.get("full_name_norm") or ""
    variants = [(v.get("doc_id"), v.get("page"), v.get("raw_masked") or v.get("raw") or "") for v in ctx.patient_name_variants]
    if not variants and patient.get("full_name"):
        variants = [(None, None, patient["full_name"])]
    scores = [{"doc_id": d, "page": p, "raw": raw, **tools.compare_names(raw, member_name)} for d, p, raw in variants if raw and member_name]
    code_score = ctx.deterministic_facts.get("name_score")  # the API's identity score is authoritative when it supplies one
    if code_score is not None:
        scores = [{**s, "score": float(code_score), "method": "api"} for s in scores] or [{"doc_id": None, "page": None, "raw": "", "score": float(code_score), "method": "api"}]
    dob_equal = None
    if patient.get("dob") and member.get("dob"):
        dob_equal = str(patient["dob"]) == str(member["dob"])
    policy_equal = ctx.deterministic_facts.get("policy_equal")
    facts = {"member_found": bool(member), "name_scores": scores, "dob_equal": dob_equal, "policy_equal": policy_equal}

    core: IdentityCore
    if ctx.docs:
        docs, trunc = _docs_block(ctx.docs, r.deps.settings.crew_max_context_tokens)
        if trunc:
            r.degraded = True
            r.warnings.append("context_truncated")
        user = f"FACTS: {json.dumps(facts, default=str)}\nEXTRACTS:\n{docs}"
        core = await r.ask(IdentityCore, "identity-v1", user)  # type: ignore[assignment]
    else:
        r.system_prompt("identity-v1")
        core = IdentityCore(reconciliation_notes="No document extracts were provided; only the code comparison is available.", insufficient_evidence=bool(variants) is False)

    # ---- enforce: code facts win over any model claim
    best = max((s["score"] for s in scores), default=None)
    worst = min((s["score"] for s in scores), default=None)
    notes = []
    for obs in core.field_observations:
        if obs.field == "name" and member_name:
            sc = float(code_score) if code_score is not None else tools.compare_names(obs.value_masked, member_name)["score"]
            if sc < 0.70 and obs.matches_record in ("match", "variation"):
                obs.matches_record = "mismatch"
                notes.append("name_observation_overridden:mismatch")
            elif sc >= 0.90 and obs.matches_record == "mismatch":
                obs.matches_record = "variation" if sc < 0.99 else "match"
                notes.append("name_observation_overridden:variation")
        if obs.field == "dob" and dob_equal is False and obs.matches_record == "match":
            obs.matches_record = "mismatch"
            notes.append("dob_observation_overridden:mismatch")
    codes = set(core.suspected_issue_codes)
    if worst is not None and worst < 0.70:
        codes.add(IdentityIssue.NAME_MISMATCH)
        codes.discard(IdentityIssue.NAME_VARIATION)
    elif worst is not None and worst >= 0.90:
        codes.discard(IdentityIssue.NAME_MISMATCH)
        if best is not None and worst < 1.0:
            codes.add(IdentityIssue.NAME_VARIATION)
        if worst >= 1.0:
            codes.discard(IdentityIssue.NAME_VARIATION)
    elif worst is not None:  # 0.70 <= worst < 0.90: a variation the reviewer must look at
        codes.discard(IdentityIssue.NAME_MISMATCH)
        codes.add(IdentityIssue.NAME_VARIATION)
    if dob_equal is False:
        codes.add(IdentityIssue.DOB_MISMATCH)
    elif dob_equal is True:
        codes.discard(IdentityIssue.DOB_MISMATCH)
    if policy_equal is False:
        codes.add(IdentityIssue.POLICY_NO_MISMATCH)
    elif policy_equal is True:
        codes.discard(IdentityIssue.POLICY_NO_MISMATCH)
    if not member:
        codes.add(IdentityIssue.MEMBER_NOT_FOUND)
    core.suspected_issue_codes = sorted(codes, key=lambda c: c.value)
    insufficient = core.insufficient_evidence or not member
    out = _base_dict(core)
    out["insufficient_evidence"] = insufficient
    r.warnings += notes
    return out


# ================================================================================================ authenticity
_QUALITY_WORDS = {"good": 0.9, "acceptable": 0.6, "poor": 0.2, "unreadable": 0.0}


def _num(v: Any) -> float | None:
    """Vision reports carry a 0-1 score or a class label (good / acceptable / poor); anything else is simply not a signal."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return _QUALITY_WORDS.get(v.strip().lower())
    return None


def _signals(ctx: AuthenticityContext) -> list[dict[str, Any]]:
    sig: list[dict[str, Any]] = []
    reports = list(ctx.vision_reports) + [{**(d.get("vision") or {}), "doc_id": d.get("doc_id")} for d in ctx.documents if d.get("vision")]
    for v in reports:
        did = v.get("doc_id")
        tamper = _num(v.get("tamper_score")) or 0.0
        if tamper >= 0.4:
            sig.append({"source": "vision", "code": "IMAGE_TAMPER_SUSPECTED", "doc_id": did, "max_severity": "blocker" if tamper >= 0.7 else "warning", "detail": f"tamper_score {tamper:.2f}"})
        font = _num(v.get("font_consistency"))
        if font is not None and font < 0.6:
            sig.append({"source": "vision", "code": "FONT_INCONSISTENCY", "doc_id": did, "max_severity": "warning", "detail": f"font_consistency {font:.2f}"})
        quality = _num(v.get("quality"))
        if quality is not None and quality < 0.5:
            sig.append({"source": "vision", "code": "LOW_QUALITY", "doc_id": did, "max_severity": "info", "detail": f"quality {quality:.2f}"})
        if v.get("signature_present") is False:
            sig.append({"source": "vision", "code": "SIGNATURE_MISSING", "doc_id": did, "max_severity": "warning", "detail": "no signature detected"})
        if v.get("template_known") is False:
            sig.append({"source": "vision", "code": "TEMPLATE_UNKNOWN", "doc_id": did, "max_severity": "info", "detail": "layout not in the known template set"})
    for s in ctx.stamp_reports:
        if s.get("stamp_present") is False:
            sig.append({"source": "stamp", "code": "STAMP_MISSING", "doc_id": s.get("doc_id"), "max_severity": "warning", "detail": "hospital stamp not found"})
        elif s.get("matches_hospital") is False:
            sig.append({"source": "stamp", "code": "STAMP_MISMATCH", "doc_id": s.get("doc_id"), "max_severity": "warning", "detail": "stamp text does not match the claiming hospital"})
    for d in ctx.documents:  # insurer-api's slim form carries the stamp flag inside ``vision``
        v = d.get("vision") or {}
        if v.get("stamp_present") is False:
            sig.append({"source": "stamp", "code": "STAMP_MISSING", "doc_id": d.get("doc_id"), "max_severity": "warning", "detail": "hospital stamp not found"})
    arith = ctx.arithmetic or (tools.get_bill_arithmetic(ctx.bill_lines) if ctx.bill_lines else None)
    if arith:
        for m in arith.get("line_mismatches", []):
            sig.append({"source": "arithmetic", "code": "ARITHMETIC_ERROR", "doc_id": None, "max_severity": "warning", "detail": f"{m.get('line_ref')}: {m.get('qty')} x {m.get('unit_price')} != {m.get('billed')}"})
        if arith.get("total_mismatch"):
            sig.append({"source": "arithmetic", "code": "ARITHMETIC_ERROR", "doc_id": None, "max_severity": "blocker", "detail": f"line total differs from the claimed total by {arith.get('total_diff')}"})
    dup = tools.find_duplicates(ctx.duplicates)
    if dup["any"]:
        sig.append({"source": "duplicate", "code": "DUPLICATE_BILL", "doc_id": None, "max_severity": "blocker", "detail": f"exact={dup['exact']} near={dup['near']}"})
    return sig


async def authenticity(ctx: AuthenticityContext, r: Runner) -> dict[str, Any]:
    signals = _signals(ctx)
    docs, trunc = _docs_block(ctx.docs, r.deps.settings.crew_max_context_tokens)
    if trunc:
        r.degraded = True
        r.warnings.append("context_truncated")
    if not signals and not ctx.docs:
        r.system_prompt("authenticity-v1")
        return {**_base_dict(AuthenticityCore(explanations=["No authenticity signals were provided."], insufficient_evidence=True))}
    core: AuthenticityCore = await r.ask(AuthenticityCore, "authenticity-v1", f"SIGNALS: {json.dumps(signals, default=str)}\nDOCUMENTS:\n{docs}")  # type: ignore[assignment]
    by_code: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        by_code.setdefault(s["code"], []).append(s)
    kept = []
    for a in core.anomalies:
        if not a.evidence:
            r.warnings.append(f"dropped_no_evidence:{a.code.value}")
            continue
        if a.source_signal == "text":
            cap = "warning"
        else:
            matches = [s for s in by_code.get(a.code.value, []) if s["source"] == a.source_signal] or by_code.get(a.code.value, [])
            if not matches:
                r.warnings.append(f"dropped_no_signal:{a.code.value}")
                continue
            cap = max((s["max_severity"] for s in matches), key=lambda x: SEV_RANK[x])
        if SEV_RANK[a.severity.value] > SEV_RANK[cap]:
            a.severity = type(a.severity)(cap)
            r.warnings.append(f"severity_capped:{a.code.value}")
        kept.append(a)
    core.anomalies = kept
    for a in core.anomalies:
        a.description, swapped = validators.neutralise(a.description)
        if swapped:
            r.warnings.append("accusatory_language_neutralised")
    core.explanations = [validators.neutralise(e)[0] for e in core.explanations]
    core.suspected_issue_codes = sorted({a.code for a in core.anomalies}, key=lambda c: c.value)
    return _base_dict(core)


# ================================================================================================ coverage
def _in_window(chunk: tools.Chunk, product: str, admitted: str | None) -> bool:
    if chunk.policy_product and product and chunk.policy_product != product:
        return False
    if admitted:
        if chunk.effective_from and chunk.effective_from > admitted:
            return False
        if chunk.effective_to and chunk.effective_to <= admitted:
            return False
    return True


def _keyword_clauses(chunks: dict[str, tools.Chunk], terms: list[str]) -> list[ClauseHit]:
    """Deterministic fallback when the model returns no grounded clause: the line of a retrieved chunk that names the
    procedure or diagnosis, quoted verbatim. Never invents text: the quote is a slice of the chunk, so it passes the same
    grounding check as a model quote. A table row is a limit, a line under an exclusions list is an exclusion."""
    hits: list[ClauseHit] = []
    wanted = [t.strip() for t in terms if len(t.strip()) >= 5]
    for ch in chunks.values():
        low_chunk = ch.text.lower()
        for line in ch.text.splitlines():
            low = line.lower()
            term = next((t for t in wanted if t.lower() in low), None)
            if term is None:
                continue
            quote = " ".join(line.split())[:400]
            if not validators.quote_in(quote, ch.text):
                continue
            exclusion = "do not pay" in low_chunk or "exclusion" in low_chunk
            effect = "excludes" if exclusion else ("limits" if "|" in line else "covers")
            hits.append(ClauseHit(clause_ref=ch.chunk_id, summary=f"Wording that mentions {term}"[:300], effect=effect,
                                  citation=Citation(chunk_id=ch.chunk_id, doc_title=ch.doc_title, section=ch.section, quote=quote)))
            break  # one clause per chunk is enough for a reviewer to follow the link
    return hits[:4]


async def coverage(ctx: CoverageContext, r: Runner) -> dict[str, Any]:
    product = (ctx.policy or {}).get("product_code") or ""
    admitted = ctx.admitted_on or (ctx.policy or {}).get("effective_date")
    names = [d.get("name") or (tools.icd_lookup(d.get("icd", "")) or {}).get("name") or d.get("icd", "") for d in ctx.diagnoses]
    names += [(tools.icd_lookup(c) or {}).get("name") or c for c in ctx.diagnosis_codes]
    names += [p.get("name") or p.get("code", "") for p in ctx.procedures] + list(ctx.procedure_codes)
    query = " ".join(n for n in names if n) + f" {product}".rstrip()
    r.system_prompt("coverage-v1")
    empty = {**_base_dict(CoverageCore(insufficient_evidence=True)), "citations": [], "no_citation": True}
    if r.deps.rag is None or not query.strip():
        r.warnings.append("rag_unavailable" if r.deps.rag is None else "no_diagnosis_or_procedure")
        return empty
    try:
        filters: dict[str, Any] = {"policy_product": product} if product else {}
        if admitted:
            filters["as_of"] = str(admitted)[:10]
        found = await r.deps.rag.search("ins_policy_wording", query, filters, 4, case_id=r.trace.case_id)
    except tools.RagUnavailable:
        r.warnings.append("rag_unavailable")
        return empty
    chunks = {c.chunk_id: c for c in found if _in_window(c, product, str(admitted)[:10] if admitted else None)}
    if not chunks:
        r.warnings.append("no_grounded_chunks")
        return empty
    block = "\n".join(f'<chunk id="{c.chunk_id}" title="{c.doc_title}" section="{c.section or ""}">\n{validators.wrap_untrusted(c.chunk_id, c.text)}\n</chunk>' for c in chunks.values())
    user = f"DIAGNOSES/PROCEDURES: {', '.join(n for n in names if n)}\nPRODUCT: {product}\nRETRIEVED CHUNKS:\n{block}"
    core: CoverageCore = await r.ask(CoverageCore, "coverage-v1", user)  # type: ignore[assignment]

    def keep(hits: list[Any]) -> list[Any]:
        out = []
        for h in hits:
            ch = chunks.get(h.citation.chunk_id)
            if ch is None or not validators.quote_in(h.citation.quote, ch.text):
                r.warnings.append(f"dropped_ungrounded:{h.clause_ref}")
                continue
            h.citation.doc_title = h.citation.doc_title or ch.doc_title
            h.citation.section = h.citation.section or ch.section
            out.append(h)
        return out

    core.applicable_clauses, core.exclusions_hit = keep(core.applicable_clauses), keep(core.exclusions_hit)
    if not core.applicable_clauses and not core.exclusions_hit:
        extra = _keyword_clauses(chunks, [n for n in names if n] + list(ctx.procedure_codes))
        if extra:
            r.warnings.append("clauses_from_keyword_fallback")
            core.applicable_clauses = [h for h in extra if h.effect != "excludes"]
            core.exclusions_hit = [h for h in extra if h.effect == "excludes"]
            core.insufficient_evidence = False
    cites = [h.citation for h in core.applicable_clauses + core.exclusions_hit]
    uniq = {(c.chunk_id, c.quote): c for c in cites}
    out = _base_dict(core)
    out["citations"] = [c.model_dump() for c in uniq.values()]
    out["no_citation"] = not uniq
    if not uniq:
        out["insufficient_evidence"] = True
    return out


# ================================================================================================ calc mapper
async def calc_mapper(ctx: CalcMapContext, r: Runner) -> dict[str, Any]:
    r.system_prompt("calc-map-v1")
    mapped: dict[str, dict[str, Any]] = {}
    pending: list[Any] = []
    for ln in ctx.lines:
        m = tools.keyword_map(ln.category, ln.description)
        if m:
            mapped[ln.line_ref] = {"line_ref": ln.line_ref, "procedure_group": None, "rationale": None, **m}
        else:
            pending.append(ln)
    unmapped: list[str] = []
    if pending:
        annex = ""
        if r.deps.rag is not None:
            try:
                cs = await r.deps.rag.search("ins_policy_wording", "non-medical items not payable annexure list", {"policy_product": ctx.product_code} if ctx.product_code else {}, 4, case_id=r.trace.case_id)
                annex = "\n".join(f'<chunk id="{c.chunk_id}">\n{validators.wrap_untrusted(c.chunk_id, c.text)}\n</chunk>' for c in cs)
            except tools.RagUnavailable:
                r.warnings.append("rag_unavailable")

        async def run_batch(batch: list[Any]) -> dict[str, dict[str, Any]]:
            payload = [{"line_ref": b.line_ref, "category": b.category, "description": b.description} for b in batch]
            core: CalcMapCore = await r.ask(CalcMapCore, "calc-map-v1", f"RETRIEVED CHUNKS:\n{annex}\nLINES:\n{json.dumps(payload, ensure_ascii=False)}")  # type: ignore[assignment]
            want = {b.line_ref for b in batch}
            got: dict[str, dict[str, Any]] = {}
            for ml in core.lines:
                if ml.line_ref in want and ml.line_ref not in got:
                    d = ml.model_dump(mode="json")
                    d["source"] = "agent"
                    got[ml.line_ref] = d
            return got

        for i in range(0, len(pending), 20):
            batch = pending[i : i + 20]
            got = await run_batch(batch)
            missing = [b for b in batch if b.line_ref not in got]
            if missing:  # re-ask once for the missing lines
                got.update(await run_batch(missing))
            for b in batch:
                if b.line_ref in got:
                    mapped[b.line_ref] = got[b.line_ref]
                else:
                    mapped[b.line_ref] = {"line_ref": b.line_ref, "mapped_group": "other", "procedure_group": None, "tags": [], "is_non_medical": False, "is_implant": False, "source": "agent", "rationale": None}
                    unmapped.append(b.line_ref)
                    r.warnings.append(f"unmapped:{b.line_ref}")
    lines = [mapped[ln.line_ref] for ln in ctx.lines]
    agent_n = sum(1 for m in lines if m["source"] == "agent")
    if lines and agent_n / len(lines) > 0.30:
        r.warnings.append("degraded_mapping_over_30pct_agent")
    return {"lines": lines, "unmapped_line_refs": unmapped}


# ================================================================================================ query drafter
def _fallback_sentence(f: Any) -> str:
    base = (f.detail or f.kind or f.key).strip().rstrip(".")
    if f.kind == "missing_document" and f.requested_doc_type:
        return f"Please upload the {f.requested_doc_type.replace('_', ' ')}: {base}."
    return f"Please clarify: {base}."


async def query_drafter(ctx: QueryDraftContext, r: Runner) -> dict[str, Any]:
    if not ctx.findings:
        raise ContextInvalid("findings must not be empty")
    keys = [f.key for f in ctx.findings]
    retrieved: dict[str, tools.Chunk] = {}
    if r.deps.rag is not None:
        for req in ctx.requirements:
            if req.citation_hint:
                try:
                    for c in await r.deps.rag.search("ins_policy_wording", f"{req.citation_hint} {req.rule}", {}, 2, case_id=r.trace.case_id):
                        retrieved[c.chunk_id] = c
                except tools.RagUnavailable:
                    r.warnings.append("rag_unavailable")
                    break
    fb = "\n".join(f"- {k}" for k in keys)
    user = (f"ROUND: {ctx.round} TONE: {ctx.tone}\nFINDINGS:\n{json.dumps([f.model_dump() for f in ctx.findings], ensure_ascii=False)}\n"
            f"REQUIREMENTS:\n{json.dumps([q.model_dump() for q in ctx.requirements], ensure_ascii=False)}\nFINDING KEYS (one sentence each):\n{fb}\n"
            f"RETRIEVED CHUNKS:\n" + "\n".join(f'<chunk id="{c.chunk_id}">\n{validators.wrap_untrusted(c.chunk_id, c.text)}\n</chunk>' for c in retrieved.values()))
    core: QueryDraftCore = await r.ask(QueryDraftCore, "query-draft-v1", user)  # type: ignore[assignment]
    missing = [k for k in keys if not (core.sentences.get(k) or "").strip()]
    if missing:  # one repair pass naming the missing keys; then code fills from the template
        core2: QueryDraftCore = await r.ask(QueryDraftCore, "query-draft-v1", user + f"\nYour previous reply omitted these finding keys: {', '.join(missing)}. Include a sentence for each.")  # type: ignore[assignment]
        core.sentences = {**core2.sentences, **{k: v for k, v in core.sentences.items() if v.strip()}}
        core.citations = core.citations or core2.citations

    allowed = validators.allowed_amounts([f.model_dump() for f in ctx.findings], [q.model_dump() for q in ctx.requirements])
    sentences: dict[str, str] = {}
    by_key = {f.key: f for f in ctx.findings}
    for k in keys:
        s = (core.sentences.get(k) or "").strip()
        s, removed = validators.strip_forbidden(s)
        if removed:
            r.warnings.append(f"forbidden_phrase:{k}")
        s, dropped = validators.reject_payables(s, allowed)
        if dropped:
            r.warnings.append(f"payable_amount_removed:{k}")
        s, inj = validators.strip_injection(s)
        if inj:
            r.warnings.append(f"injection_suspected:{k}")
        s = " ".join(s.split())[:220].strip()
        if not s:
            s = _fallback_sentence(by_key[k])
            r.warnings.append(f"template_sentence:{k}")
        sentences[k] = s

    req_types = {q.doc_type for q in ctx.requirements} | {f.requested_doc_type for f in ctx.findings if f.requested_doc_type}
    doc_types = [DocType(t) for t in sorted(req_types) if t in DocType._value2member_map_]
    cites = []
    for c in core.citations:
        ch = retrieved.get(c.chunk_id)
        if ch is None or not validators.quote_in(c.quote, ch.text):
            r.warnings.append(f"dropped_ungrounded:{c.chunk_id}")
            continue
        cites.append(c.model_dump())

    def render(sents: dict[str, str]) -> str:
        numbered = "\n".join(f"{i}) {sents[k]}" for i, k in enumerate(keys, start=1))
        docs_line = f"Documents requested: {', '.join(t.value.replace('_', ' ') for t in doc_types)}.\n" if doc_types else ""
        deadline = f"Please respond {ctx.deadline_text.strip().rstrip('.')}.\n" if (ctx.round >= 3 or ctx.tone == "firm") and ctx.deadline_text else ""
        return TEMPLATE.read_text(encoding="utf-8").format(hospital_name=ctx.hospital_name, numbered_findings=numbered, documents_line=docs_line, deadline_line=deadline).strip()

    text = render(sentences)
    if len(text) > 1200:  # shorten non-blocker sentences first, keep every blocker
        for k in keys:
            if len(text) <= 1200:
                break
            if by_key[k].severity != "blocker":
                sentences[k] = _fallback_sentence(by_key[k])[:120]
                text = render(sentences)
        if len(text) > 1200:
            raise AgentInvalidOutput([{"msg": "query text over 1,200 characters even after shortening"}])
    tone = validators.tone_check(text)  # recomputed by code; overrides anything the model claims
    ref = ctx.claim_ref or "this claim"
    return {"subject": f"Claim {ref}: additional information required (Round {ctx.round})", "text": text, "requested_doc_types": [t.value for t in doc_types], "finding_keys": keys,
            "citations": cites, "tone_check": tone}


# ================================================================================================ triage
async def triage(ctx: TriageContext, r: Runner) -> dict[str, Any]:
    open_keys = [f.key for f in ctx.open_findings]
    missing_docs = list(ctx.code_check.get("requested_missing", []))
    r.system_prompt("triage-v1")
    has_content = bool(ctx.response_text_masked.strip()) or bool(ctx.attached_docs)
    if open_keys and has_content:
        docs, trunc = _docs_block(ctx.attached_docs, r.deps.settings.crew_max_context_tokens)
        if trunc:
            r.degraded = True
            r.warnings.append("context_truncated")
        user = (f"OPEN FINDINGS: {json.dumps([f.model_dump() for f in ctx.open_findings], ensure_ascii=False)}\nCODE CHECK: {json.dumps(ctx.code_check)}\n"
                f"HOSPITAL RESPONSE:\n{validators.wrap_untrusted('response', ctx.response_text_masked)}\nATTACHED DOCUMENTS:\n{docs}")
        core: TriageCore = await r.ask(TriageCore, "triage-v1", user)  # type: ignore[assignment]
        resolved, remaining, verdict, notes = list(core.resolved_finding_keys), list(core.remaining_finding_keys), core.verdict, core.notes
    else:
        resolved, remaining, verdict, notes = [], list(open_keys), "unresolved", "The response contained no text or attachments to assess."
    # ---- enforce: sets must partition the open keys exactly
    allowed = set(open_keys)
    resolved = [k for k in dict.fromkeys(resolved) if k in allowed]
    remaining = [k for k in dict.fromkeys(remaining) if k in allowed and k not in resolved]
    for k in open_keys:
        if k not in resolved and k not in remaining:
            remaining.append(k)
            r.warnings.append(f"finding_added_to_remaining:{k}")
    # a finding whose required document is missing cannot be resolved
    need_doc = {f.key for f in ctx.open_findings if f.requested_doc_type in missing_docs}
    moved = [k for k in resolved if k in need_doc]
    if moved:
        resolved = [k for k in resolved if k not in need_doc]
        remaining += [k for k in moved if k not in remaining]
        r.warnings.append("resolved_overridden_missing_document")
    if verdict == "off_topic":
        resolved, remaining = [], list(open_keys)
    else:
        verdict = "resolved" if open_keys and not remaining else ("unresolved" if not resolved else "partially_resolved")
        if verdict == "resolved" and missing_docs:
            verdict = "partially_resolved" if resolved else "unresolved"
            r.warnings.append("verdict_overridden_missing_document")
    notes, removed = validators.strip_forbidden(notes)
    if removed:
        r.warnings.append("forbidden_phrase")
    notes, _ = validators.reject_payables(notes, validators.allowed_amounts(ctx.model_dump()))
    return {"verdict": verdict, "resolved_finding_keys": resolved, "remaining_finding_keys": remaining, "notes": notes[:400], "missing_doc_types": missing_docs}


# ================================================================================================ supervisor
def _has_blocker(obj: Any) -> bool:
    if isinstance(obj, dict):
        if obj.get("severity") == "blocker" or obj.get("blocking") is True or obj.get("degraded") is True or obj.get("failure"):
            return True
        if isinstance(obj.get("blockers"), int | float) and obj["blockers"] > 0:
            return True
        return any(_has_blocker(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_blocker(v) for v in obj)
    return False


async def supervisor(ctx: SupervisorContext, r: Runner) -> dict[str, Any]:
    if not ctx.step_outputs:
        raise ContextInvalid("step_outputs must not be empty")
    user = "STEP OUTPUTS:\n" + validators.wrap_untrusted("steps", json.dumps(ctx.step_outputs, ensure_ascii=False, default=str)[: r.deps.settings.crew_max_context_tokens * 3])
    core: SupervisorCore = await r.ask(SupervisorCore, "supervisor-v1", user)  # type: ignore[assignment]
    summary, _ = validators.neutralise(core.summary_for_reviewer)
    summary, removed = validators.strip_forbidden(summary)
    if removed:
        r.warnings.append("forbidden_phrase")
    summary, dropped = validators.reject_payables(summary, set())  # the supervisor never quotes amounts
    if dropped:
        r.warnings.append("amount_removed_from_summary")
    summary, inj = validators.strip_injection(summary)
    if inj:
        r.warnings.append("injection_suspected")
    action = core.recommended_next_action
    if action == "proceed" and _has_blocker(ctx.step_outputs):
        action = "manual_review"
        r.warnings.append("proceed_downgraded_blocker_present")
    return {"summary_for_reviewer": summary[:1000] or "No summary available; review the step outputs directly.", "disagreements": [d.model_dump() for d in core.disagreements], "recommended_next_action": action}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    path: str
    context_model: type[BaseModel]
    fn: Any
    prompt: str
    kind: str  # smart | fast
    uses_rag: bool = False


SPECS: dict[str, AgentSpec] = {s.name: s for s in [
    AgentSpec("identity", "/v1/identity/analyze", IdentityContext, identity, "identity-v1", "smart"),
    AgentSpec("authenticity", "/v1/authenticity/analyze", AuthenticityContext, authenticity, "authenticity-v1", "smart"),
    AgentSpec("coverage", "/v1/coverage/analyze", CoverageContext, coverage, "coverage-v1", "smart", True),
    AgentSpec("calc_mapper", "/v1/calc/map-lines", CalcMapContext, calc_mapper, "calc-map-v1", "fast", True),
    AgentSpec("query_drafter", "/v1/query/draft", QueryDraftContext, query_drafter, "query-draft-v1", "smart", True),
    AgentSpec("triage", "/v1/query/triage", TriageContext, triage, "triage-v1", "fast"),
    AgentSpec("supervisor", "/v1/supervisor/summarize", SupervisorContext, supervisor, "supervisor-v1", "smart"),
]}


def alias_for(spec: AgentSpec, settings: Settings, override: str | None = None) -> str:
    return override or os.environ.get(f"INS_CREW_ALIAS_{spec.name.upper()}") or (settings.alias_smart if spec.kind == "smart" else settings.alias_fast)


_ = (asyncio, date, re, ALL_CORES, ALL_OUTPUTS)
