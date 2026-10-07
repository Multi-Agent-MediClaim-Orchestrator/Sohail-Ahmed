"""Pure query-loop logic (03-05 §6): builder, lint, due dates, template fallback, triage cross-checks. No I/O."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config.schemas import QueryPolicy
from ..verification.schemas import Finding

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "queries"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
SECTIONS: dict[str, dict[str, str]] = yaml.safe_load((TEMPLATE_DIR / "_sections.yaml").read_text(encoding="utf-8"))
CATEGORY_PRIORITY = ["missing_document", "identity_mismatch", "billing_discrepancy", "medical_clarification", "illegible_document", "policy_exclusion", "other"]


# ------------------------------------------------------------------ due dates
def add_hours(start: datetime, hours: int, skip_weekends: bool = False) -> datetime:
    due = start + timedelta(hours=hours)
    if skip_weekends:
        while due.weekday() >= 5:  # Sat/Sun shifts forward to Monday, same clock time
            due += timedelta(days=1)
    return due


def due_for_round(now: datetime, rnd: int, policy: QueryPolicy, *, acked_at: datetime | None = None) -> datetime:
    """Round SLA runs from the hospital's delivery ack when known, else from the send time (03-05 §5 task 15)."""
    hours = policy.round_sla_hours[min(rnd, len(policy.round_sla_hours)) - 1]
    return add_hours(acked_at or now, hours, policy.skip_weekends)


# ------------------------------------------------------------------ builder
@dataclass
class QueryDraft:
    category: str
    finding_keys: list[str]
    requested_doc_types: list[str]
    dedupe_key: str
    round: int
    sections: list[tuple[str, list[str], list[str]]] = field(default_factory=list)  # (category, finding keys, doc types)


def dedupe_key(keys: list[str], docs: list[str], rnd: int) -> str:
    return hashlib.sha256(("|".join(sorted(keys) + sorted(docs)) + f"|r{rnd}").encode()).hexdigest()


def build_queries(findings: list[Finding], rnd: int, max_per_round: int = 1, resolved_doc_types: set[str] | None = None) -> list[QueryDraft]:
    """Group fixable, unresolved, non-overridden blockers by category; consolidate into ``max_per_round`` messages."""
    resolved = resolved_doc_types or set()
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        if f.severity.value != "blocker" or not f.fixable or f.overridden:
            continue
        docs = {d.value for d in f.suggested_doc_types}
        if docs and docs <= resolved and f.code == "completeness.missing_required":
            continue  # the hospital already supplied it: re-verification closes the finding, no query
        groups.setdefault((f.suggested_query_category.value if f.suggested_query_category else "other"), []).append(f)
    drafts: list[QueryDraft] = []
    for cat in sorted(groups, key=lambda c: CATEGORY_PRIORITY.index(c) if c in CATEGORY_PRIORITY else 99):
        fs = groups[cat]
        keys = sorted({f.key or "" for f in fs})
        doc_list = sorted({d.value for f in fs for d in f.suggested_doc_types})
        drafts.append(QueryDraft(cat, keys, doc_list, dedupe_key(keys, doc_list, rnd), rnd, [(cat, keys, doc_list)]))
    if not drafts:
        return []
    if len(drafts) <= max_per_round:
        return drafts
    # consolidate: first (highest priority) category carries the message, others become sections
    head = drafts[0]
    for extra in drafts[1:]:
        head.finding_keys = sorted(set(head.finding_keys) | set(extra.finding_keys))
        head.requested_doc_types = sorted(set(head.requested_doc_types) | set(extra.requested_doc_types))
        head.sections.extend(extra.sections)
    head.dedupe_key = dedupe_key(head.finding_keys, head.requested_doc_types, rnd)
    return [head]


def render_template(sections: list[tuple[str, list[str], list[str]]], *, hospital_name: str, claim_ref: str, insurer_claim_no: str, rnd: int, due_by: datetime) -> str:
    items = []
    for cat, _, docs in sections:
        sec = SECTIONS.get(cat, SECTIONS["other"])
        items.append({"title": sec["title"], "body": sec["body"].format(docs=", ".join(d.replace("_", " ") for d in docs) or "as applicable")})
    all_docs = sorted({d for _, _, ds in sections for d in ds})
    return _env.get_template("_layout.j2").render(
        hospital_name=hospital_name, claim_ref=claim_ref, insurer_claim_no=insurer_claim_no, round=rnd, sections=items,
        doc_list=", ".join(d.replace("_", " ") for d in all_docs), due_text=due_by.strftime("%d %b %Y %H:%M UTC"),
    ).strip()


# ------------------------------------------------------------------ lint
@dataclass(frozen=True)
class LintError:
    code: str
    detail: str


PII_RES = [re.compile(r"(?<![\w-])\d{4}\s?\d{4}\s?\d{4}(?![\w-])"), re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), re.compile(r"(?<![\w.])(?:\+91[\s-]?)?[6-9]\d{9}\b"),
           re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")]
PROMISES = ["will be approved", "guaranteed", "assured payment", "we will pay", "claim is approved", "will be paid", "payment is assured"]
BLAME = ["fraud", "forged", "fake", "lying", "cheat", "negligent", "your fault"]
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s?[\d,]+(?:\.\d+)?", re.I)


def lint_query(text: str, *, requested_doc_types: list[str], allowed_doc_types: set[str], finding_keys: list[str], max_len: int = 1200,
               citations: list[dict[str, Any]] | None = None, known_clause_ids: set[str] | None = None, allowed_amount_strings: set[str] | None = None,
               has_due_and_contact: bool = True) -> list[LintError]:
    errs: list[LintError] = []
    low = text.lower()
    if any(p.search(text) for p in PII_RES):
        errs.append(LintError("LINT_PII", "ID number, phone or e-mail found"))
    for c in citations or []:
        if c.get("type") == "clause" and known_clause_ids is not None and c.get("ref") not in known_clause_ids:
            errs.append(LintError("LINT_CITATION", f"clause {c.get('ref')} not found in the knowledge base"))
    if any(p in low for p in PROMISES):
        errs.append(LintError("LINT_PROMISE", "contains a payment/approval promise"))
    if len(text) > max_len:
        errs.append(LintError("LINT_LENGTH", f"{len(text)} > {max_len} characters"))
    if any(w in low for w in BLAME):
        errs.append(LintError("LINT_TONE", "accusatory wording"))
    if has_due_and_contact and not ("respond by" in low and "contact" in low):
        errs.append(LintError("LINT_TONE", "must state the due date and a contact line"))
    bad = [d for d in requested_doc_types if d not in allowed_doc_types]
    if bad:
        errs.append(LintError("LINT_DOCS", f"document types not allowed: {bad}"))
    if allowed_amount_strings is not None:
        for m in AMOUNT_RE.findall(text):
            if m.strip().lower() not in allowed_amount_strings:
                errs.append(LintError("LINT_SCOPE", f"amount {m!r} is not quoted from a finding"))
    mentioned = {d for d in re.findall(r"[a-z]+(?: [a-z]+)*", low)}
    _ = (mentioned, finding_keys)
    return errs


# ------------------------------------------------------------------ triage
@dataclass
class Triage:
    verdict: str  # sufficient | partial | insufficient | off_topic
    resolved_finding_keys: list[str]
    remaining_finding_keys: list[str]
    notes: str = ""
    source: str = "rules"


def apply_triage(agent: Triage, *, query_finding_keys: list[str], requested_doc_types: list[str], attached_doc_types: set[str], answer_text: str,
                 attached_count: int, finding_keys_for_doc: dict[str, list[str]] | None = None) -> Triage:
    """Server rules after the crew returns (03-05 §6.4): an agent cannot resolve what the code still sees as open."""
    resolved = [k for k in agent.resolved_finding_keys if k in set(query_finding_keys)]
    remaining = [k for k in dict.fromkeys([*agent.remaining_finding_keys, *[k for k in query_finding_keys if k not in resolved]])]
    verdict = agent.verdict
    missing = sorted(set(requested_doc_types) - attached_doc_types)
    if missing and verdict == "sufficient":
        verdict = "partial"
        for d in missing:
            remaining += (finding_keys_for_doc or {}).get(d, [])
    if not answer_text.strip() and not attached_count:
        verdict = "insufficient"
    remaining = [k for k in dict.fromkeys(remaining) if k not in resolved]
    return Triage(verdict, resolved, remaining, agent.notes, agent.source)


def rules_triage(*, query_finding_keys: list[str], requested_doc_types: list[str], attached_doc_types: set[str], answer_text: str, attached_count: int) -> Triage:
    """LLM-down fallback: sufficient only if every requested doc type was classified in the attachments."""
    missing = set(requested_doc_types) - attached_doc_types
    if not answer_text.strip() and not attached_count:
        return Triage("insufficient", [], list(query_finding_keys), "no answer and no documents", "rules")
    if requested_doc_types and not missing:
        return Triage("sufficient", list(query_finding_keys), [], "all requested document types attached", "rules")
    if requested_doc_types and len(missing) < len(set(requested_doc_types)):
        return Triage("partial", [], list(query_finding_keys), f"still missing: {sorted(missing)}", "rules")
    if not requested_doc_types and answer_text.strip():
        return Triage("partial", [], list(query_finding_keys), "explanation provided; verification decides", "rules")
    return Triage("insufficient", [], list(query_finding_keys), f"missing: {sorted(missing)}", "rules")


def ensure_footer(text: str, due_by: datetime, insurer_claim_no: str) -> str:
    """Human-authored text gets the mandatory due-date + contact line when missing (lint LINT_TONE)."""
    low = text.lower()
    if "respond by" in low and "contact" in low:
        return text
    return f"{text.rstrip()}" + chr(10) + f"Please respond by {due_by.strftime('%d %b %Y %H:%M UTC')}. For questions please contact the claims desk quoting {insurer_claim_no}."
