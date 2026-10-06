"""Pure completeness evaluation (doc 04 §6): no I/O, no clock, deterministic output.

Statuses: present_ok | missing | unusable | needs_review | waived | not_applicable | pending_processing
Severities: info | warning | review | blocker. A case is complete when there is no blocker, no review item
and nothing still pending; warnings never block."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz

from app.completeness import messages
from app.completeness.context import CaseContext, DocFacts
from app.completeness.schemas import (
    Alternative,
    DocRequirementsConfig,
    Item,
    Result,
    Rule,
    Severity,
)

BLOCKING_FLAGS = frozenset(
    {"blurry", "cropped", "unreadable", "black_page", "wrong_orientation_unfixable"}
)
WARNING_FLAGS = frozenset({"low_resolution", "glare"})
BILL_TYPES = {"pharmacy_bill", "final_bill", "itemised_bill", "procedure_bill"}
EXEMPT_PRE_ADMISSION = {"lab_report", "radiology_report", "investigation_report", "prescription"}
HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "smt", "shri", "sri", "shrimati", "master", "baby"}

ALIASES: dict[str, tuple[str, ...]] = {
    "patient_name": ("patient_name", "patient.name", "name"),
    "date": ("date", "bill_date", "document_date", "prescription_date"),
    "total": ("total", "grand_total", "net_amount", "total_amount"),
    "lines": ("lines", "line_items", "items"),
    "medicines": ("medicines", "medications", "drugs", "rx"),
    "doctor_signature": ("doctor_signature", "signature", "signed_by"),
}
SEVERITY_RANK = {"info": 0, "warning": 1, "review": 2, "blocker": 3}


# ------------------------------------------------------------------------------------------ helpers
def lookup(data: dict[str, Any] | None, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def field_value(d: DocFacts, name: str) -> Any:
    for key in ALIASES.get(name, (name,)):
        v = lookup(d.typed_json, key)
        if v not in (None, "", [], {}):
            return v
    return None


def has_field(d: DocFacts, name: str) -> bool:
    return field_value(d, name) is not None


def parse_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    for pat in (r"^(\d{4})-(\d{2})-(\d{2})", r"^(\d{2})[/-](\d{2})[/-](\d{4})$"):
        m = re.match(pat, s)
        if m:
            a, b, c = (int(x) for x in m.groups())
            try:
                return date(a, b, c) if pat.startswith(r"^(\d{4})") else date(c, b, a)
            except ValueError:
                return None
    return None


def parse_amount(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    cleaned = re.sub(r"[₹,\s]|rs\.?|inr|/-", "", str(v).lower())
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def norm_name(s: Any) -> str:
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    words = re.sub(r"[^a-z\s]", " ", t).split()
    return " ".join(w for w in words if w not in HONORIFICS)


def name_ratio(a: Any, b: Any) -> int:
    na, nb = norm_name(a), norm_name(b)
    return (
        int(fuzz.token_sort_ratio(na, nb)) if na and nb else 100
    )  # nothing to compare -> no complaint


def match(allowed: list[str] | None, value: str | None) -> bool:
    return allowed is None or "*" in allowed or value in allowed


def applies(a: Any, ctx: CaseContext) -> bool:
    return (
        match(a.claim_type, ctx.claim_type)
        and match(a.admission_type, ctx.admission_type)
        and match(a.procedure_group, ctx.procedure_group)
        and (not a.flags or bool(set(a.flags) & ctx.flags))
    )


def _rank_doc(d: DocFacts) -> tuple[Any, ...]:
    blocking = len(d.quality_flags & BLOCKING_FLAGS)
    # sort descending by usable, fewer blocking flags, quality, recency; doc_id breaks ties deterministically
    return (d.usable_state == "ok", -blocking, d.quality_score, d.uploaded_at, d.doc_id)


def pick_best(docs: list[DocFacts]) -> list[DocFacts]:
    return sorted(docs, key=_rank_doc, reverse=True)


# ------------------------------------------------------------------------------- per-document check
class DocCheck:
    __slots__ = ("blocking", "review", "warn", "pending", "stamp_warn")

    def __init__(self) -> None:
        self.blocking: list[str] = []
        self.review: list[str] = []
        self.warn: list[str] = []
        self.pending = False
        self.stamp_warn = False


def check_doc(rule: Rule, d: DocFacts, cfg: DocRequirementsConfig, ctx: CaseContext) -> DocCheck:
    c = DocCheck()
    if d.usable_state == "pending_processing":
        c.pending = True
        return c
    c.blocking += sorted(d.quality_flags & BLOCKING_FLAGS)
    c.warn += sorted(d.quality_flags & WARNING_FLAGS)
    if rule.must_have_stamp:
        if d.has_required_stamp is False:
            c.blocking.append("stamp_missing")
        elif d.has_required_stamp is None:
            if (ctx.now - d.uploaded_at).total_seconds() > ctx.stamp_grace_s:
                c.review.append("stamp_not_evaluated")
            else:
                c.pending = True
        else:
            sr = cfg.stamp_rules.get(d.doc_type)  # type: ignore[call-overload]
            floor = sr.min_stamp_confidence if sr else 0.0
            if d.stamp_confidence is not None and d.stamp_confidence < floor:
                c.review.append("stamp_low_confidence")
                c.stamp_warn = True
    minimum = (
        rule.min_parse_confidence
        if rule.min_parse_confidence is not None
        else ctx.gates.get("parse_min", 0.75)
    )
    if d.parse_confidence is None or d.parse_confidence < minimum:
        c.review.append("low_parse_confidence")
    if d.agreement_score is not None and d.agreement_score < ctx.gates.get("agreement_min", 0.9):
        c.review.append("passes_disagree")
    for f in rule.must_have_fields:
        if not has_field(d, f):
            (c.blocking if rule.requirement == "required" else c.review).append(
                f"field_missing:{f}"
            )
    return c


def _item_from_checks(
    rule: Rule, docs: list[DocFacts], checks: list[DocCheck], via: str | None
) -> Item:
    ids = [d.doc_id for d in docs]
    blocking = sorted({r for c in checks for r in c.blocking})
    review = sorted({r for c in checks for r in c.review})
    warn = sorted({r for c in checks for r in c.warn})
    base = dict(
        rule_id=rule.id,
        doc_type=rule.doc_type.value,
        requirement=rule.requirement,
        document_ids=ids,
        condition=rule.condition,
        via=via,
    )
    if blocking:
        return Item(
            **base,
            status="unusable",
            severity="blocker",
            reasons=blocking + review,
            message=messages.render(rule.doc_type.value, blocking[0]),
        )
    if any(c.pending for c in checks):
        return Item(**base, status="pending_processing", severity="info", reasons=[], message="")
    if review:
        only_stamp = review == ["stamp_low_confidence"]
        return Item(
            **base,
            status="needs_review",
            severity="warning" if only_stamp else "review",
            reasons=review,
            message=messages.render(rule.doc_type.value, review[0]),
        )
    if warn:
        return Item(**base, status="present_ok", severity="warning", reasons=warn)
    return Item(**base, status="present_ok", severity="info")


def _alternative_docs(
    rule: Rule, ctx: CaseContext, alts: list[Alternative]
) -> tuple[list[DocFacts], str | None]:
    for alt in alts:
        if alt.rule_id != rule.id:
            continue
        if alt.any_of:
            for t in alt.any_of:
                cands = [
                    d for d in ctx.docs_by_type.get(t.value, []) if d.usable_state != "excluded"
                ]
                if cands:
                    return pick_best(cands), t.value
        elif alt.all_of:
            sets = [
                [d for d in ctx.docs_by_type.get(t.value, []) if d.usable_state != "excluded"]
                for t in alt.all_of
            ]
            if all(sets):
                return [d for s in sets for d in pick_best(s)], "+".join(
                    t.value for t in alt.all_of
                )
    return [], None


def evaluate_rule(rule: Rule, ctx: CaseContext, cfg: DocRequirementsConfig) -> Item | None:
    if not applies(rule.applies, ctx):
        wants_group = rule.applies.procedure_group and "*" not in rule.applies.procedure_group
        # only when the router says codes are still to come; plain medical admissions have none, legitimately
        if wants_group and ctx.procedure_group is None and "procedure_codes_pending" in ctx.flags:
            return Item(
                rule_id=rule.id,
                doc_type=rule.doc_type.value,
                requirement=rule.requirement,
                status="not_applicable",
                severity="info",
                condition=rule.condition,
                message="procedure codes are pending",
            )
        return None
    if rule.id in ctx.waivers:
        return Item(
            rule_id=rule.id,
            doc_type=rule.doc_type.value,
            requirement=rule.requirement,
            status="waived",
            severity="info",
            condition=rule.condition,
            message=ctx.waivers[rule.id].reason,
        )
    all_docs = ctx.docs_by_type.get(rule.doc_type.value, [])
    cands = pick_best([d for d in all_docs if d.usable_state != "excluded"])
    via = None
    if not cands:
        cands, via = _alternative_docs(rule, ctx, cfg.alternatives)
    if not cands:
        if rule.requirement == "optional":
            return Item(
                rule_id=rule.id,
                doc_type=rule.doc_type.value,
                requirement="optional",
                status="missing",
                severity="info",
                reasons=["not_uploaded"],
                condition=rule.condition,
            )
        reason = (
            "file_rejected"
            if all_docs and all(d.excluded_reason == "infected" for d in all_docs)
            else "not_uploaded"
        )
        return Item(
            rule_id=rule.id,
            doc_type=rule.doc_type.value,
            requirement=rule.requirement,
            status="missing",
            severity="blocker",
            reasons=["not_uploaded"],
            condition=rule.condition,
            message=messages.render(rule.doc_type.value, reason),
        )
    chosen = cands if not rule.any_candidate else cands[:1]
    checks = [check_doc(rule, d, cfg, ctx) for d in chosen]
    return _item_from_checks(rule, cands, checks, via)


# -------------------------------------------------------------------------------- cross-document checks
def _docs(ctx: CaseContext) -> list[DocFacts]:
    out = [d for ds in ctx.docs_by_type.values() for d in ds if d.usable_state == "ok"]
    return sorted(out, key=lambda d: (d.doc_type, d.doc_id))


def _x(rule_id: str, status: str, severity: Severity, reason: str, docs: list[DocFacts]) -> Item:
    t = docs[0].doc_type if len(docs) == 1 else None
    return Item(
        rule_id=rule_id,
        doc_type=t,
        requirement="check",
        status=status,
        severity=severity,  # type: ignore[arg-type]
        reasons=[reason],
        document_ids=sorted(d.doc_id for d in docs),
        message=messages.render(t, reason),
    )


def cross_checks(ctx: CaseContext, cfg: DocRequirementsConfig) -> list[Item]:
    items: list[Item] = []
    docs = _docs(ctx)
    adm = ctx.admission
    # X-01 patient name across documents
    bad = [
        d
        for d in docs
        if (n := field_value(d, "patient_name")) is not None
        and name_ratio(n, ctx.patient.name) < ctx.name_match_min
    ]
    if bad:
        items.append(_x("X-01", "needs_review", "warning", "patient_name_mismatch", bad))
    # X-02 DOB across documents (blocker when two confident documents disagree)
    dobs = {
        d.doc_id: parse_date(lookup(d.typed_json, "dob") or lookup(d.typed_json, "patient.dob"))
        for d in docs
    }
    present = {k: v for k, v in dobs.items() if v}
    if len(set(present.values())) > 1:
        involved = [d for d in docs if d.doc_id in present]
        confident = [d for d in involved if (d.parse_confidence or 0) >= 0.9]
        distinct_conf = {present[d.doc_id] for d in confident}
        sev: Severity = "blocker" if len(distinct_conf) > 1 else "warning"
        items.append(
            _x(
                "X-02",
                "unusable" if sev == "blocker" else "needs_review",
                sev,
                "dob_mismatch",
                involved,
            )
        )
    # X-03 admission / discharge dates on the discharge summary vs the case (±1 day for emergencies)
    tol = timedelta(days=1 if ctx.admission_type == "emergency" else 0)
    bad = []
    for d in docs:
        if d.doc_type != "discharge_summary":
            continue
        a, b = (
            parse_date(lookup(d.typed_json, "admitted_on")),
            parse_date(lookup(d.typed_json, "discharged_on")),
        )
        if (a and adm.admitted_on and abs(a - adm.admitted_on) > tol) or (
            b and adm.discharged_on and abs(b - adm.discharged_on) > tol
        ):
            bad.append(d)
    if bad:
        items.append(_x("X-03", "needs_review", "warning", "admission_dates_mismatch", bad))
    # X-04 bill total vs line items (a total below the sum of its lines is a blocker)
    bad = []
    for d in docs:
        if d.doc_type not in BILL_TYPES:
            continue
        total = parse_amount(field_value(d, "total"))
        lines = field_value(d, "lines")
        if total is None or not isinstance(lines, list):
            continue
        amounts = [
            parse_amount(lookup(ln, "amount")) if isinstance(ln, dict) else None for ln in lines
        ]
        if all(a is not None for a in amounts) and total + Decimal("0.01") < sum(
            amounts, Decimal("0")
        ):  # type: ignore[arg-type]
            bad.append(d)
    if bad:
        items.append(_x("X-04", "unusable", "blocker", "bill_total_mismatch", bad))
    # X-05 document dates inside the admission window (pre-admission records get 30 days of grace)
    bad = []
    if adm.admitted_on and adm.discharged_on:
        lo, hi = adm.admitted_on - timedelta(days=1), adm.discharged_on + timedelta(days=1)
        for d in docs:
            dt = parse_date(field_value(d, "date"))
            if dt is None or lo <= dt <= hi:
                continue
            if (
                d.doc_type in EXEMPT_PRE_ADMISSION
                and adm.admitted_on - timedelta(days=30) <= dt < lo
            ):
                continue
            bad.append(d)
    if bad:
        items.append(_x("X-05", "needs_review", "warning", "date_out_of_window", bad))
    # X-06 policy number on the policy card equals the declared one
    declared = re.sub(r"[\s-]", "", ctx.patient.policy_number or "").lower()
    bad = [
        d
        for d in docs
        if d.doc_type == "policy_card"
        and declared
        and (pn := lookup(d.typed_json, "policy_number")) is not None
        and re.sub(r"[\s-]", "", str(pn)).lower() != declared
    ]
    if bad:
        items.append(_x("X-06", "needs_review", "warning", "policy_number_mismatch", bad))
    # X-07 hospital name consistent across documents that carry one
    named = [
        (d, lookup(d.typed_json, "hospital_name"))
        for d in docs
        if lookup(d.typed_json, "hospital_name")
    ]
    if len(named) > 1 and any(name_ratio(named[0][1], n) < 80 for _, n in named[1:]):
        items.append(
            _x("X-07", "needs_review", "warning", "hospital_name_mismatch", [d for d, _ in named])
        )
    return items


def ordering_item(ctx: CaseContext, cfg: DocRequirementsConfig) -> Item | None:
    o = cfg.ordering
    if not o or not o.chronological:
        return None
    rx = [
        parse_date(field_value(d, "date"))
        for d in ctx.docs_by_type.get("prescription", [])
        if d.usable_state == "ok"
    ]
    rx = [x for x in rx if x]
    if not rx:
        return None
    first_rx = min(rx)
    bad = [
        d
        for t in sorted(BILL_TYPES)
        for d in ctx.docs_by_type.get(t, [])
        if d.usable_state == "ok" and (dt := parse_date(field_value(d, "date"))) and dt < first_rx
    ]
    if not bad:
        return None
    return Item(
        rule_id=o.rule_id,
        doc_type=None,
        requirement="check",
        status="needs_review",
        severity=o.severity,
        reasons=["not_chronological"],
        document_ids=sorted(d.doc_id for d in bad),
        message=messages.render(None, "not_chronological"),
    )


# ----------------------------------------------------------------------------------------------- main
def evaluate(ctx: CaseContext, cfg: DocRequirementsConfig) -> Result:
    items: list[Item] = []
    for rule in cfg.rules:
        it = evaluate_rule(rule, ctx, cfg)
        if it is not None:
            items.append(it)
    if ctx.unclassified_ids:
        items.append(
            Item(
                rule_id="R-CLS-01",
                doc_type=None,
                requirement="check",
                status="needs_review",
                severity="review",
                reasons=["classification_low_confidence"],
                document_ids=sorted(ctx.unclassified_ids),
                message=messages.render(None, "classification_low_confidence"),
            )
        )
    o = ordering_item(ctx, cfg)
    if o:
        items.append(o)
    items += cross_checks(ctx, cfg)
    items.sort(key=lambda i: (i.rule_id, i.doc_type or ""))
    blocks = any(
        i.severity in ("blocker", "review") or i.status == "pending_processing" for i in items
    )
    provisional = any(i.status in ("pending_processing", "not_applicable") for i in items)
    return Result(complete=not blocks, provisional=provisional, items=items)
