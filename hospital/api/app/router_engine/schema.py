"""router_rules config validation (doc 05 §3, §3.2)."""

from __future__ import annotations

from typing import Any

from app.router_engine.predicates import KNOWN_FACTS, validate_cond

CLAIM_TYPES = {"cashless", "reimbursement"}
KNOWN_STEPS = {
    "preauth_check",
    "completeness",
    "claim_build",
    "officer_signoff",
    "submit",
    "filing_window_check",
    "intimation_check",
}
KNOWN_FLAGS = {
    "implant",
    "high_value",
    "medico_legal",
    "day_care",
    "maternity",
    "preauth_issue",
    "late_filing",
    "provisional",
}


def validate_rules(p: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for key in ("claim_type_rules", "emergency_rules", "flag_rules", "step_templates"):
        if key not in p:
            errs.append(f"missing {key}")
    if errs:
        return errs
    extra = set(p) - {"claim_type_rules", "emergency_rules", "flag_rules", "step_templates"}
    errs += [f"unknown top-level key {k!r}" for k in sorted(extra)]
    rules = p["claim_type_rules"]
    if not isinstance(rules, list) or not rules:
        errs.append("claim_type_rules must be a non-empty list")
    else:
        for i, r in enumerate(rules):
            if not isinstance(r, dict) or set(r) != {"if", "then"}:
                errs.append(f"claim_type_rules[{i}] must have exactly 'if' and 'then'")
                continue
            errs += validate_cond(r["if"], f"claim_type_rules[{i}].if")
            if r["then"] not in CLAIM_TYPES:
                errs.append(f"claim_type_rules[{i}].then must be one of {sorted(CLAIM_TYPES)}")
            is_default = isinstance(r["if"], dict) and "default" in r["if"]
            if is_default and i != len(rules) - 1:
                errs.append("'default' is allowed only in the last claim_type rule")
        last = rules[-1]
        if not (
            isinstance(last, dict)
            and isinstance(last.get("if"), dict)
            and last["if"].get("default") is True
        ):
            errs.append("the last claim_type rule must be {'if': {'default': true}, ...}")
    em = p["emergency_rules"]
    if (
        not isinstance(em, dict)
        or not isinstance(em.get("intimation_hours"), int)
        or em["intimation_hours"] <= 0
    ):
        errs.append("emergency_rules.intimation_hours must be a positive integer")
    elif not isinstance(em.get("signals"), list) or not all(
        isinstance(s, str) for s in em["signals"]
    ):
        errs.append("emergency_rules.signals must be a list of strings")
    else:
        for s in em["signals"]:
            if "=" not in s and "." not in s:
                errs.append(f"emergency signal {s!r} must look like 'fact=value' or 'doc.flag'")
    if not isinstance(p["flag_rules"], list):
        errs.append("flag_rules must be a list")
    else:
        for i, fr in enumerate(p["flag_rules"]):
            if not isinstance(fr, dict) or set(fr) != {"flag", "if"}:
                errs.append(f"flag_rules[{i}] must have exactly 'flag' and 'if'")
                continue
            if fr["flag"] not in KNOWN_FLAGS:
                errs.append(f"flag_rules[{i}].flag {fr['flag']!r} is not a known flag")
            errs += validate_cond(fr["if"], f"flag_rules[{i}].if")
    st = p["step_templates"]
    if not isinstance(st, dict) or set(st) != CLAIM_TYPES:
        errs.append("step_templates must define exactly cashless and reimbursement")
    else:
        for ct, steps in st.items():
            if not isinstance(steps, list) or not steps or not set(steps) <= KNOWN_STEPS:
                errs.append(
                    f"step_templates.{ct} must be a list of known steps {sorted(KNOWN_STEPS)}"
                )
            elif len(set(steps)) != len(steps):
                errs.append(f"step_templates.{ct} has duplicate steps")
    return errs


__all__ = ["KNOWN_FACTS", "validate_rules"]
