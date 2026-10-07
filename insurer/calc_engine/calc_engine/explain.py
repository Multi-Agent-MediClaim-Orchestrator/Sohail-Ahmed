"""Human-readable trace (07 §9.5)."""

from __future__ import annotations

from decimal import Decimal

from .models import CalcResult


def inr(d: Decimal) -> str:
    """Indian-style grouping is avoided on purpose: plain thousands separators keep goldens stable."""
    return f"{d:,.2f}"


def explain(res: CalcResult, case_id: str = "") -> str:
    out = [f"ENGINE {res.engine_version}  RULES v{res.rules_version}  case {case_id[:8]}"]
    by_ref = {ln.line_ref: ln for ln in res.lines}
    for t in res.trace:
        delta = t.after_total - t.before_total
        if t.step in ("S6", "S11") or delta == 0 and not t.affected_lines:
            if t.step == "S6":
                out.append(f"S6  eligible subtotal  {inr(t.after_total)}")
            continue
        parts = []
        for ref in t.affected_lines:
            for h in by_ref[ref].rule_trace:
                if h.step == t.step:
                    parts.append(f"{h.rule_id} {ref} -{inr(h.amount)}")
        out.append(f"{t.step:<3} {t.description.lower()[:28]:<28} {inr(t.before_total)} -> {inr(t.after_total)}  ({inr(delta)})  " + "; ".join(parts))
    flags = ", ".join(f"{f.code.value}" for f in res.flags) or "-"
    blocked = f"  BLOCKED {res.blocked.value}" if res.blocked else ""
    out.append(f"PAYABLE {inr(res.payable_total)}   PATIENT PAYS {inr(res.patient_pays_total)}   FLAGS: {flags}{blocked}")
    return "\n".join(out)
