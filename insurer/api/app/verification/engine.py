"""Deterministic step checks (03-03 §6). Pure functions: no I/O, no clock. Evidence over confidence — agents only
explain; every gating number comes from here."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from claim_contract.enums import DocType, QueryCategory, Severity

from .codes import CODES, info
from .context import DocInfo, VCtx
from .groups import specific_group
from .names import name_similarity
from .outcome import StepOutcome, outcome_from
from .schemas import Evidence, Finding, evidence_ref, finding_key

ONE_DAY = timedelta(days=1)
ARITH_TOLERANCE = Decimal("1.00")


def make_finding(code: str, step: str, *, severity: str | None = None, fixable: bool | None = None, evidence: list[Evidence] | None = None,
                 detail: str | None = None, doc_types: list[str] | None = None, message: str | None = None) -> Finding:
    ci = info(code)
    f = Finding(
        code=code, severity=Severity(severity or ci.severity), message=(message or ci.message)[:500], evidence=evidence or [],
        fixable=ci.fixable if fixable is None else fixable,
        suggested_query_category=QueryCategory(ci.category) if (ci.category and (ci.fixable if fixable is None else fixable)) else None,
        suggested_doc_types=[DocType(d) for d in (doc_types if doc_types is not None else ci.doc_types)], detail=detail,
    )
    f.key = finding_key(step, f.code, evidence_ref(f))
    return f


def doc_ev(d: DocInfo, field: str | None = None, page: int | None = None) -> Evidence:
    return Evidence(doc_id=d.id, page=page, field=field)


# ------------------------------------------------------------------ completeness
def _condition_holds(ctx: VCtx, when: dict[str, Any]) -> bool:
    for k, v in when.items():
        if k == "bill_has_category" and v not in ctx.bill_categories:
            return False
        if k == "claim_type" and ctx.claim_type != v:
            return False
        if k == "admission_type" and ctx.admission_type != v:
            return False
        if k == "medico_legal" and bool(v) != (ctx.admission_type == "emergency" and ctx.is_accident):
            return False
        if k not in ("bill_has_category", "claim_type", "admission_type", "medico_legal"):
            return False  # unknown predicate: never silently satisfied
    return True


def required_doc_types(ctx: VCtx) -> set[str]:
    req = {d.value for d in ctx.docs_cfg.required}
    for rule in ctx.docs_cfg.conditional:
        if _condition_holds(ctx, rule.when):
            req |= set(rule.require)
    return req


def check_completeness(ctx: VCtx) -> StepOutcome:
    required = required_doc_types(ctx)
    have = {d.doc_type for d in ctx.fetched_docs}
    findings: list[Finding] = []
    for t in sorted(required - have):
        findings.append(make_finding("completeness.missing_required", "completeness", doc_types=[t], detail=t))
    low: list[str] = []
    gate = ctx.docs_cfg.min_parse_confidence
    for d in ctx.fetched_docs:
        if d.parse_confidence is not None and d.parse_confidence < gate:
            low.append(d.id)
            findings.append(make_finding("completeness.low_parse_confidence", "completeness", evidence=[doc_ev(d)], doc_types=[d.doc_type]))
        if d.doc_type == "discharge_summary" and d.extract and d.extract.get("signed") is False:
            findings.append(make_finding("completeness.unsigned_discharge", "completeness", evidence=[doc_ev(d, "signature")]))
    det = {"required": sorted(required), "present": sorted(have & required), "missing": sorted(required - have), "low_confidence": sorted(low)}
    return outcome_from(findings, None, det)


# ------------------------------------------------------------------ identity
def policy_active_on(ctx: VCtx) -> bool:
    p = ctx.policy
    if p is None:
        return False
    if p.status in ("cancelled", "suspended"):
        return False
    if not (p.start_date <= ctx.admitted_on <= p.end_date):
        return False
    if p.premium_paid_until and ctx.admitted_on > p.premium_paid_until + timedelta(days=p.grace_days):
        return False
    return p.status in ("active", "lapsed")


def check_identity(ctx: VCtx) -> StepOutcome:
    th = ctx.thresholds
    if ctx.policy is None:
        f = make_finding("identity.policy_not_found", "identity", detail="policy")
        return outcome_from([f], 0.0, {"name_similarity": None, "dob_match": None, "gender_match": None, "id_hash_match": None,
                                       "policy_active_on_admission": False}, hard_fail=True)
    if ctx.member is None:
        f = make_finding("identity.member_not_found", "identity", detail="member")
        return outcome_from([f], 0.0, {"name_similarity": None, "dob_match": None, "gender_match": None, "id_hash_match": None,
                                       "policy_active_on_admission": policy_active_on(ctx)}, hard_fail=True)
    mem, pat = ctx.member, ctx.patient
    sim = name_similarity(pat.full_name, mem.full_name)
    dob_ok = pat.dob == mem.dob
    gender_ok = pat.gender == mem.gender
    idh_ok = None if not (pat.id_proof_hash and mem.id_proof_hash) else pat.id_proof_hash.lower() == mem.id_proof_hash.lower()
    score = round(0.5 * sim + 0.3 * float(dob_ok) + 0.1 * float(gender_ok) + 0.1 * (1.0 if idh_ok in (True, None) else 0.0), 3)
    findings: list[Finding] = []
    if th.dob_must_match and not dob_ok:
        findings.append(make_finding("identity.dob_mismatch", "identity", evidence=[Evidence(field="dob")]))
    if sim < th.name_similarity_min:
        sev = "blocker" if sim < 0.7 else "warning"
        findings.append(make_finding("identity.name_mismatch", "identity", severity=sev, fixable=(sev == "blocker"), evidence=[Evidence(field="patient_name")]))
    if not gender_ok:
        findings.append(make_finding("identity.gender_mismatch", "identity", evidence=[Evidence(field="gender")]))
    if idh_ok is False:
        findings.append(make_finding("identity.id_hash_mismatch", "identity", evidence=[Evidence(field="id_proof")]))
    if score < th.identity_min_score and not any(f.severity is Severity.blocker for f in findings):
        findings.append(make_finding("identity.low_score", "identity"))
    det = {"name_similarity": sim, "dob_match": dob_ok, "gender_match": gender_ok, "id_hash_match": idh_ok, "policy_active_on_admission": policy_active_on(ctx)}
    return outcome_from(findings, score, det)


# ------------------------------------------------------------------ authenticity (rules only; the agent adds warnings)
def check_authenticity_rules(ctx: VCtx) -> StepOutcome:
    f: list[Finding] = []
    th = ctx.thresholds
    arithmetic_ok = True
    for d in ctx.fetched_docs:
        if d.doc_type in ("final_bill", "itemised_bill") and d.extract and d.extract.get("total") is not None:
            if abs(Decimal(str(d.extract["total"])) - ctx.claimed_gross) > ARITH_TOLERANCE:
                arithmetic_ok = False
                f.append(make_finding("auth.bill_arithmetic", "authenticity", evidence=[doc_ev(d, "total")], doc_types=[d.doc_type]))
    dates_ok = True
    for d in ctx.fetched_docs:
        for raw in (d.extract or {}).get("dates", []) or []:
            from datetime import date as _date

            dt = _date.fromisoformat(raw)
            if not (ctx.admitted_on - ONE_DAY <= dt <= ctx.discharged_on + ONE_DAY):
                dates_ok = False
                f.append(make_finding("auth.dates_inconsistent", "authenticity", evidence=[doc_ev(d, "date")], detail=raw))
    if ctx.overlapping:
        f.append(make_finding("auth.duplicate_claim", "authenticity", evidence=[Evidence(field=o.claim_no) for o in ctx.overlapping]))
    if ctx.shared_hash_docs:
        f.append(make_finding("auth.duplicate_document", "authenticity", evidence=[Evidence(doc_id=i) for i in ctx.shared_hash_docs]))
    empanelled = True
    if ctx.claim_type == "cashless" and ctx.hospital.network_status != "network":
        empanelled = False
    if ctx.hospital.empanelment_valid_till and ctx.hospital.empanelment_valid_till < ctx.admitted_on:
        empanelled = False
    if not empanelled:
        f.append(make_finding("auth.hospital_not_empanelled", "authenticity", evidence=[Evidence(field=ctx.hospital.code)]))
    stamp_docs = [d for d in ctx.fetched_docs if d.doc_type in {t.value for t in ctx.docs_cfg.stamp_required_for}]
    stamp_present: bool | None = None
    signature_present: bool | None = None
    for d in stamp_docs:
        if not d.vision:
            continue
        stamp_present = (stamp_present is not False) and bool(d.vision.get("stamp_detected"))
        signature_present = (signature_present is not False) and bool(d.vision.get("signature_detected", True))
        if not d.vision.get("stamp_detected"):
            f.append(make_finding("auth.stamp_missing", "authenticity", evidence=[doc_ev(d, "stamp")], doc_types=[d.doc_type]))
        if not d.vision.get("signature_detected", True):
            f.append(make_finding("auth.signature_missing", "authenticity", evidence=[doc_ev(d, "signature")], doc_types=[d.doc_type]))
    tamper = [0.0]
    for d in ctx.fetched_docs:
        ts = (d.vision or {}).get("tamper_score")
        if ts is not None:
            tamper.append(float(ts))
            if float(ts) > (1 - th.authenticity_floor):
                f.append(make_finding("auth.tamper_suspected", "authenticity", evidence=[doc_ev(d, "tamper")]))
    score = round(1 - max(tamper), 3)
    if score < th.authenticity_warn_floor and not any(x.code == "auth.tamper_suspected" for x in f):
        f.append(make_finding("auth.fraud_warning", "authenticity"))
    det = {"arithmetic_ok": arithmetic_ok, "dates_consistent": dates_ok, "duplicate_claim_ids": sorted(o.claim_no for o in ctx.overlapping),
           "hospital_empanelled": empanelled, "stamp_present": stamp_present, "signature_present": signature_present}
    return outcome_from(dedupe(f), score, det)


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out = []
    for x in findings:
        k = x.key or finding_key("x", x.code, evidence_ref(x))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ------------------------------------------------------------------ coverage
def check_coverage(ctx: VCtx) -> StepOutcome:
    pol, mem = ctx.policy, ctx.member
    assert pol is not None and mem is not None, "coverage requires identity to have resolved policy and member"
    rules = ctx.rules.rules if ctx.rules else None
    f: list[Finding] = []
    adm = ctx.admitted_on
    active = True
    if pol.status in ("cancelled", "suspended"):
        active = False
        f.append(make_finding("coverage.policy_inactive", "coverage", detail=pol.status))
    elif pol.premium_paid_until and adm > pol.premium_paid_until:
        if adm <= pol.premium_paid_until + timedelta(days=pol.grace_days):
            f.append(make_finding("coverage.policy_grace", "coverage"))
        else:
            active = False
            f.append(make_finding("coverage.policy_inactive", "coverage", detail="premium_lapsed"))
    within = pol.start_date <= adm <= pol.end_date and adm >= mem.cover_start
    if not within:
        f.append(make_finding("coverage.outside_period", "coverage"))
    days_cover = (adm - mem.cover_start).days
    remaining_wait = 0
    pre_hit = False
    exclusion_hits: list[str] = []
    if rules:
        w = rules.waiting_periods_days
        if days_cover < w.initial and not ctx.is_accident:
            remaining_wait = max(remaining_wait, w.initial - days_cover)
            f.append(make_finding("coverage.waiting_period", "coverage", detail="initial"))
        for dx in ctx.diagnosis_codes:
            for pe in mem.pre_existing:
                if dx.startswith(pe) and days_cover < w.pre_existing:
                    pre_hit = True
                    remaining_wait = max(remaining_wait, w.pre_existing - days_cover)
                    partial = pe in rules.pre_existing_group_map  # the engine disallows only the mapped group
                    f.append(make_finding("coverage.pre_existing", "coverage", severity="warning" if partial else "blocker", detail=dx))
            grp = specific_group(dx)
            if grp and grp in w.specific and days_cover < w.specific[grp]:
                remaining_wait = max(remaining_wait, w.specific[grp] - days_cover)
                f.append(make_finding("coverage.waiting_period", "coverage", severity="warning", detail=grp))  # engine blocks the group's lines only
            if any(dx.startswith(p) for p in rules.exclusions.icd_prefixes):
                exclusion_hits.append(dx)
                f.append(make_finding("coverage.exclusion", "coverage", detail=dx))
    remaining = pol.sum_insured + pol.bonus - ctx.utilised
    if remaining <= 0:
        f.append(make_finding("coverage.sum_insured_exhausted", "coverage"))
    elif remaining < ctx.claimed_amount:
        f.append(make_finding("coverage.sum_insured_low", "coverage", detail=f"remaining {remaining}"))
    if ctx.claim_type == "cashless" and ctx.hospital.network_status != "network":
        f.append(make_finding("coverage.not_network", "coverage"))
    det = {"policy_active": active, "within_period": within, "waiting_period_days_remaining": remaining_wait, "pre_existing_hit": pre_hit,
           "exclusion_hits": sorted(exclusion_hits), "network_status": ctx.hospital.network_status}
    return outcome_from(dedupe(f), None, det)


# ------------------------------------------------------------------ re-verification planning
INVALIDATES: dict[str, list[str]] = {
    "discharge_summary": ["completeness", "identity", "authenticity", "coverage"],
    "final_bill": ["completeness", "authenticity", "calculation"],
    "itemised_bill": ["completeness", "authenticity", "calculation"],
    "pharmacy_bill": ["completeness", "authenticity", "calculation"],
    "id_proof": ["completeness", "identity"],
    "policy_card": ["completeness", "identity"],
    "claim_form": ["completeness", "identity"],
    "implant_sticker": ["completeness", "authenticity", "calculation"],
    "lab_report": ["completeness", "authenticity"],
    "radiology_report": ["completeness", "authenticity"],
    "investigation_report": ["completeness", "authenticity"],
    "admission_note": ["completeness", "authenticity"],
    "preauth_approval": ["completeness", "coverage"],
    "payment_receipt": ["completeness"],
    "cancelled_cheque": ["completeness"],
    "fir_mlc": ["completeness", "coverage"],
    "other": ["completeness"],
}
_ORDER = ["completeness", "identity", "authenticity", "coverage", "calculation"]


def plan_rerun(changed_doc_types: list[str]) -> list[str]:
    """Minimal ordered step list for a set of changed document types (downstream steps follow their inputs)."""
    steps: set[str] = {"document_fetch"} if changed_doc_types else set()
    for t in changed_doc_types:
        steps |= set(INVALIDATES.get(t, ["completeness"]))
    if "identity" in steps:
        steps |= {"coverage", "calculation"}
    if "coverage" in steps:
        steps.add("calculation")
    order = ["document_fetch", *_ORDER]
    return [s for s in order if s in steps]


_ = CODES
