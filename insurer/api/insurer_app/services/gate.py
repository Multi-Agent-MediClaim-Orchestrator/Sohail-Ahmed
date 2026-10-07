"""Pure decision gate (03-04 §3, §6.1) + the user's auto-approval tier. No I/O, no clock, no env: fuzzable.

Tiers
  auto            : clean claim, payable <= T_auto, no review flags  -> approved without a human click  (user decision)
  reviewer        : <= T_auto, not eligible for auto (warnings, etc.) -> one Reviewer confirms
  single_approver : > T_auto (<= T_four) or any review flag           -> one Approver / Senior reviewer
  dual_approver   : > T_four, or a rejection of a claim > T_four       -> two distinct people, >= 1 senior
Equality at a threshold stays in the lower tier (strict ``>``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Tier = Literal["auto", "reviewer", "single_approver", "dual_approver"]


@dataclass(frozen=True)
class Gate:
    tier: Tier
    required: int
    roles: tuple[str, ...]
    min_senior: int
    gate_amount: Decimal
    flags: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Thresholds:
    t_auto: Decimal
    t_four: Decimal
    allow_auto: bool = True
    allow_reviewer_final: bool = True
    auto_min_identity: float = 0.90


@dataclass(frozen=True)
class AutoFacts:
    """Facts the auto tier needs; all computed by code from the verification run (never from agent text)."""

    outcome: str  # approve | partial | reject
    active_blockers: int
    active_warnings: int
    identity_score: float | None
    manual_verification: bool
    degraded: bool


def auto_eligible(f: AutoFacts, th: Thresholds, flags: list[str]) -> tuple[bool, list[str]]:
    why: list[str] = []
    if not th.allow_auto:
        why.append("auto_approval_disabled")
    if f.outcome not in ("approve", "partial"):
        why.append("only_approvals_can_be_automatic")
    if f.active_blockers:
        why.append("blockers_present")
    if f.active_warnings:
        why.append("warnings_present")
    if f.identity_score is None or f.identity_score < th.auto_min_identity:
        why.append("identity_below_threshold")
    if f.manual_verification or f.degraded:
        why.append("degraded_or_manual_verification")
    if flags:
        why.append("review_flags")
    return (not why), why


def compute_gate(claimed: Decimal, payable: Decimal, outcome: str, th: Thresholds, flags: list[str], auto: AutoFacts | None = None) -> Gate:
    amount = max(claimed, payable)
    big_reject = outcome == "reject" and claimed > th.t_four
    fl = tuple(sorted(flags))
    if amount > th.t_four or big_reject:
        return Gate("dual_approver", 2, ("approver", "senior_reviewer"), 1, amount, fl, ("amount_above_t_four" if amount > th.t_four else "large_claim_rejection",))
    if amount > th.t_auto:
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount, fl, ("amount_above_t_auto",))
    if flags:
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount, fl, ("review_flags",))
    if auto is not None:
        ok, why = auto_eligible(auto, th, flags)
        if ok:
            return Gate("auto", 0, (), 0, amount, fl, ("clean_claim_within_t_auto",))
        if th.allow_reviewer_final:
            return Gate("reviewer", 1, ("reviewer", "approver", "senior_reviewer"), 0, amount, fl, tuple(why))
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount, fl, tuple(why))
    if not th.allow_reviewer_final:
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount, fl, ("reviewer_tier_disabled",))
    return Gate("reviewer", 1, ("reviewer", "approver", "senior_reviewer"), 0, amount, fl, ())


def explain_gate(g: Gate, th: Thresholds) -> str:
    """Text shown by the UI. The UI never computes a tier itself."""
    if g.tier == "auto":
        return f"Clean claim, gate amount {g.gate_amount} <= T_auto {th.t_auto}: approved automatically."
    if g.tier == "reviewer":
        return f"gate amount {g.gate_amount} <= T_auto {th.t_auto}: one reviewer confirms" + (f" (not automatic: {', '.join(g.reasons)})" if g.reasons else "") + "."
    if g.tier == "single_approver":
        why = f"gate amount {g.gate_amount} > T_auto {th.t_auto}" if g.gate_amount > th.t_auto else f"review flag(s): {', '.join(g.flags)}"
        return f"{why}. One approver or senior reviewer must approve."
    return f"gate amount {g.gate_amount} > T_four {th.t_four}" + (" (or rejection of a large claim)" if g.gate_amount <= th.t_four else "") + ". Two approvers including one senior reviewer are required."
