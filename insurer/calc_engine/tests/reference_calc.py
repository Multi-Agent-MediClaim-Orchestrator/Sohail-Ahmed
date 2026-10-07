"""Independent, naive reference implementation (07 §9.4). Written from the spec; imports NOTHING from calc_engine.

Works on the JSON-shaped input dict with ``fractions.Fraction`` and rounds only where the spec says so. Tolerance
against the engine: <= 1 cent x number of lines."""

from __future__ import annotations

from datetime import date
from fractions import Fraction as F
from typing import Any


def D(x: Any) -> F:
    return F(str(x))


def rnd(x: F) -> F:
    c = x * 100
    n = (2 * c.numerator + c.denominator) // (2 * c.denominator)
    return F(n, 100)


def age(dob: str, on: str) -> int:
    d, o = date.fromisoformat(dob), date.fromisoformat(on)
    return o.year - d.year - ((o.month, o.day) < (d.month, d.day))


def reference(inp: dict[str, Any]) -> dict[str, Any]:
    r, p, m, a = inp["rules"], inp["policy"], inp["member"], inp["admission"]
    adm, dis = date.fromisoformat(a["admitted_on"]), date.fromisoformat(a["discharged_on"])
    lines = inp["lines"]
    claimed = {ln["line_ref"]: D(ln["claimed_amount"]) for ln in lines}
    allowed = dict(claimed)
    flags: set[str] = set()
    blocked = None

    def block_all(reason: str) -> None:
        nonlocal blocked
        blocked = reason
        for k in allowed:
            allowed[k] = F(0)

    remaining = max(F(0), D(p["sum_insured"]) + D(p["bonus_sum"]) - D(p["utilised_this_year"]))
    pstart, pend = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    paid_until = date.fromisoformat(p["premium_paid_until"])
    cover = date.fromisoformat(m["cover_start"])
    if p["status"] != "active":
        block_all("POLICY_INACTIVE")
    elif paid_until < adm and (adm - paid_until).days > p["grace_days"]:
        block_all("PREMIUM_LAPSED")
    elif paid_until < adm:
        flags.add("PREMIUM_IN_GRACE")
    if blocked is None and (not (pstart <= adm <= pend) or adm < cover):
        block_all("OUTSIDE_COVER")
    days_cover = (adm - cover).days
    dx = a["diagnosis_codes"]
    if blocked is None:
        accident = a["admission_type"] == "emergency" and any(c.startswith(x) for c in dx for x in r["accident_icd_prefixes"])
        w = r["waiting_periods_days"]
        if days_cover < w["initial"] and not accident:
            block_all("INITIAL_WAITING")
    if blocked is None:
        for pe in m["pre_existing"]:
            if any(c.startswith(pe["icd_prefix"]) for c in dx) and days_cover < r["waiting_periods_days"]["pre_existing"]:
                groups = r["pre_existing_group_map"].get(pe["icd_prefix"])
                if groups:
                    for ln in lines:
                        if ln.get("procedure_group") in groups:
                            allowed[ln["line_ref"]] = F(0)
                    flags.add("WAITING_PERIOD_HIT")
                else:
                    block_all("PRE_EXISTING_WAITING")
                    break
        if blocked is None:
            for g, d in r["waiting_periods_days"]["specific"].items():
                if a.get("procedure_group") == g and days_cover < d:
                    for ln in lines:
                        if ln.get("procedure_group") == g:
                            allowed[ln["line_ref"]] = F(0)
                    flags.add("WAITING_PERIOD_HIT")

    def scale_group(refs: list[str], limit: F) -> None:
        tot = sum((allowed[k] for k in refs), F(0))
        if tot > limit and tot > 0:
            for k in refs:
                allowed[k] = allowed[k] * limit / tot

    def room_and_sublimits() -> None:
        def sublimits() -> None:
            for g, lim in r["sub_limits"].items():
                refs = [ln["line_ref"] for ln in lines if ln.get("procedure_group") == g and allowed[ln["line_ref"]] > 0]
                scale_group(refs, D(lim))

        def room() -> None:
            day_care = a.get("day_care") or a.get("procedure_group") in r["day_care_groups"]
            if day_care:
                return
            ratios: list[F] = []
            for ln in lines:
                if ln["mapped_group"] not in ("room_rent", "icu") or allowed[ln["line_ref"]] <= 0:
                    continue
                rr = r["room_rent"]
                pct = D(rr["icu_percent"] if ln["mapped_group"] == "icu" else rr["percent"])
                cap = rnd(D(p["sum_insured"]) * pct / 100)
                if rr.get("per_day_cap") is not None:
                    cap = min(cap, D(rr["per_day_cap"]))
                tier = a["hospital"].get("room_rent_tier")
                if rr.get("use_tier_caps") and tier and tier in rr.get("tier_caps", {}):
                    cap = min(cap, D(rr["tier_caps"][tier]))
                rate = D(ln["unit_price"])
                days = ln.get("days") or max(1, (dis - adm).days)
                if rate > cap:
                    allowed[ln["line_ref"]] = min(allowed[ln["line_ref"]], cap * days)
                    ratios.append(cap / rate)
                    if ln.get("days") is None:
                        flags.add("ROOM_DAYS_ASSUMED")
                elif ln.get("days") is None:
                    flags.add("ROOM_DAYS_ASSUMED")
            if r["proportionate_deduction"] and ratios:
                k = min(ratios)
                for ln in lines:
                    g = ln["mapped_group"]
                    if g in r["proportionate_applies_to"] and g not in r["proportionate_exempt"]:
                        allowed[ln["line_ref"]] *= k

        for f in (sublimits, room) if r["order_profile"] == "standard" else (room, sublimits):
            f()

    if blocked is None:
        ex = r["exclusions"]
        primary = dx[0] if dx else None
        for ln in lines:
            ref = ln["line_ref"]
            if allowed[ref] <= 0:
                continue
            icd = primary is not None and any(primary.startswith(x) for x in ex["icd_prefixes"]) and (
                a.get("procedure_group") is None or ln.get("procedure_group") == a.get("procedure_group"))
            tag = bool(set(ln.get("exclusion_tags", [])) & set(ex["tags"]))
            nm = (ln.get("is_non_medical") and ex["non_medical_policy"] == "exclude_all") or (
                ex["non_medical_policy"] == "exclude_listed" and "non_medical" in ln.get("exclusion_tags", []))
            if icd or tag or nm:
                allowed[ref] = F(0)
        room_and_sublimits()
        for g, cap in r["line_caps"].items():
            refs = [ln["line_ref"] for ln in lines if ln["mapped_group"] == g and allowed[ln["line_ref"]] > 0]
            scale_group(refs, D(cap))
        w = r["hospitalisation_windows"]
        for ln in lines:
            ref = ln["line_ref"]
            if ln["mapped_group"] in ("pre_hospitalisation", "post_hospitalisation") and allowed[ref] > 0:
                if ln.get("service_date") is None:
                    flags.add("NO_DATE_ON_LINE")
                    continue
                sd = date.fromisoformat(ln["service_date"])
                if ln["mapped_group"] == "pre_hospitalisation" and (adm - sd).days > w["pre_days"]:
                    allowed[ref] = F(0)
                if ln["mapped_group"] == "post_hospitalisation" and (sd - dis).days > w["post_days"]:
                    allowed[ref] = F(0)
        eligible = sum(allowed.values(), F(0))
        if eligible == 0:
            blocked = "ALL_LINES_EXCLUDED"
        else:
            cands: list[F] = []
            cp = r["co_pay"]
            cond = cp["conditions"].get("age_gte")
            if D(cp["percent"]) > 0 and (cond is None or age(m["dob"], a["admitted_on"]) >= cond):
                cands.append(D(cp["percent"]))
            if a["hospital"]["network_status"] == "non_network" and D(r["non_network_co_pay_percent"]) > 0:
                cands.append(D(r["non_network_co_pay_percent"]))
            pct = F(0) if not cands else (min(F(100), sum(cands, F(0))) if r["stack_co_pay"] else max(cands))
            if cands:
                flags.add("CO_PAY_SELECTED")
            ded_amt = D(r["deductible"]["amount"])
            if r["co_pay_order"] == "after_deductible":
                ded = min(ded_amt, eligible)
                copay = rnd((eligible - ded) * pct / 100)
            else:
                copay = rnd(eligible * pct / 100)
                ded = min(ded_amt, eligible - copay)
            pre = eligible - ded - copay
            payable = min(pre, remaining)
            if payable < pre:
                flags.add("SUM_INSURED_CAPPED" if remaining > 0 else "")
                if remaining == 0:
                    blocked = "SUM_INSURED_EXHAUSTED"
            flags.discard("")
            return {"payable": rnd(payable), "blocked": blocked, "flags": flags, "claimed": sum(claimed.values(), F(0))}
    return {"payable": F(0), "blocked": blocked, "flags": flags, "claimed": sum(claimed.values(), F(0))}
