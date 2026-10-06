"""Pure route decision (doc 05 §5.2-5.3): same facts + rules -> identical decision, no I/O, no clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.router_engine.predicates import TRUE, UNKNOWN, Tri, holds

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class RouteDecision:
    pipeline: str
    admission_type: str
    flags: list[str]
    procedure_group: str | None
    required_steps: list[str]
    provisional: bool
    rules_output: dict[str, Any]
    intimation_deadline: str | None = None
    filing_deadline: str | None = None
    preauth_by: str | None = None
    approximate: bool = False
    reminders: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "admission_type": self.admission_type,
            "flags": self.flags,
            "procedure_group": self.procedure_group,
            "required_steps": self.required_steps,
            "provisional": self.provisional,
            "rules_output": self.rules_output,
            "intimation_deadline": self.intimation_deadline,
            "filing_deadline": self.filing_deadline,
            "preauth_by": self.preauth_by,
            "approximate": self.approximate,
            "reminders": self.reminders,
        }


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def signal_holds(sig: str, facts: dict[str, Any]) -> Tri:
    """'admission_source=ER' -> equality; 'admission_note.contains_emergency' -> flag in the note's flag set."""
    if "=" in sig:
        fact, val = sig.split("=", 1)
        v = facts.get(fact)
        return UNKNOWN if v is None else Tri(str(v) == val)
    doc, flag = sig.split(".", 1)
    flags = facts.get(f"{doc}_text_flags")
    if flags is None:
        return UNKNOWN
    return Tri(flag in flags)


def compute_deadlines(
    pipeline: str,
    adm: str,
    facts: dict[str, Any],
    rules: dict[str, Any],
    dl: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "intimation_deadline": None,
        "filing_deadline": None,
        "preauth_by": None,
        "approximate": False,
        "reminders": [],
        "late": False,
        "provisional": False,
    }
    admitted: datetime | None = facts.get("admitted_at")
    discharged: datetime | None = facts.get("discharged_at")
    if facts.get("admitted_at_estimated"):
        out["approximate"] = True
    if pipeline == "reimbursement":
        if discharged is None:
            out["provisional"] = True
        else:
            days = int(dl.get("reimbursement_filing_days", 30))
            end_local = datetime.combine(
                discharged.astimezone(IST).date() + timedelta(days=days), time(23, 59, 59), IST
            )
            dead = end_local.astimezone(UTC)
            out["filing_deadline"] = iso(dead)
            out["filing_date"] = end_local.date()
            out["reminders"] = [
                {"kind": "filing_deadline", "at": iso(dead - d)}
                for d in (timedelta(days=7), timedelta(days=2), timedelta(hours=6))
            ]
            out["late"] = now > dead
    if pipeline == "cashless" and adm == "emergency" and admitted is not None:
        out["intimation_deadline"] = iso(
            admitted + timedelta(hours=int(rules["emergency_rules"]["intimation_hours"]))
        )
    if pipeline == "cashless" and adm == "planned" and admitted is not None:
        out["preauth_by"] = iso(
            admitted - timedelta(hours=int(dl.get("planned_preauth_lead_hours", 48)))
        )
    return out


def decide(
    facts: dict[str, Any],
    rules: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    deadlines: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> RouteDecision:
    now = now or datetime.now(UTC)
    provisional = False
    claim_type: str | None = None
    for r in rules["claim_type_rules"]:
        t = holds(r["if"], facts)
        if t.v is True:
            claim_type = r["then"]
            break
        if t.v is None:
            provisional = True
    assert claim_type is not None, "validation guarantees a trailing default rule"  # noqa: S101
    sigs = [signal_holds(s, facts) for s in rules["emergency_rules"]["signals"]]
    adm = "emergency" if any(s.v is True for s in sigs) else "planned"
    provisional |= adm == "planned" and any(s.v is None for s in sigs)
    flags: set[str] = set()
    for fr in rules["flag_rules"]:
        t = holds(fr["if"], facts)
        if t.v is True:
            flags.add(fr["flag"])
        elif t.v is None:
            provisional = True
    if facts.get("preauth_issue"):
        flags.add("preauth_issue")
    rules_out = {"pipeline": claim_type, "admission_type": adm, "flags": sorted(flags)}
    ov = overrides or {}
    claim_type = ov.get("claim_type", claim_type)
    adm = ov.get("admission_type", adm)
    flags = (flags | set(ov.get("flags_add", []))) - set(ov.get("flags_remove", []))
    steps = list(rules["step_templates"][claim_type])
    if adm == "emergency" and claim_type == "cashless":
        steps.append("intimation_check")
    dl = compute_deadlines(claim_type, adm, facts, rules, deadlines or {}, now)
    provisional |= dl["provisional"]
    if dl["late"]:
        flags.add("late_filing")
    if provisional:
        flags.add("provisional")
    return RouteDecision(
        pipeline=claim_type,
        admission_type=adm,
        flags=sorted(flags),
        procedure_group=None
        if facts.get("procedure_group") in (None, "none")
        else facts["procedure_group"],
        required_steps=steps,
        provisional=provisional,
        rules_output=rules_out,
        intimation_deadline=dl["intimation_deadline"],
        filing_deadline=dl["filing_deadline"],
        preauth_by=dl["preauth_by"],
        approximate=dl["approximate"],
        reminders=dl["reminders"],
    )


def decision_json(d: RouteDecision, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**d.to_dict(), **(extra or {})}


__all__ = ["RouteDecision", "TRUE", "decide"]
