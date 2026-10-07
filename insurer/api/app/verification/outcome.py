"""Step status rules (03-03 §3) and run finalisation (§6.4) — pure, exhaustively tested."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schemas import Finding


@dataclass
class StepOutcome:
    status: str  # passed | flagged | failed
    score: float | None
    findings: list[Finding]
    deterministic: dict[str, Any] = field(default_factory=dict)


def step_status(findings: list[Finding], *, hard_fail: bool = False) -> str:
    """blocker+unfixable -> failed; blocker+fixable -> flagged; only warnings/info or none -> passed.
    Overridden findings are excluded."""
    active = [f for f in findings if not f.overridden]
    blockers = [f for f in active if f.severity.value == "blocker"]
    if hard_fail or any(not f.fixable for f in blockers):
        return "failed"
    if blockers:
        return "flagged"
    return "passed"


def outcome_from(findings: list[Finding], score: float | None = None, deterministic: dict[str, Any] | None = None, *, hard_fail: bool = False) -> StepOutcome:
    return StepOutcome(step_status(findings, hard_fail=hard_fail), score, findings, deterministic or {})


@dataclass
class RunDecision:
    """Result of ``decide_run``: where the case goes next and what is recommended."""

    next_status: str  # needs_info | ready_for_decision
    recommendation: str | None  # approve | partial | reject | None (needs_info)
    reason_codes: list[str]
    fixable: list[Finding]
    unfixable: list[Finding]
    manual_verification: bool = False


def decide_run(
    findings: list[Finding], *, calc_payable: Any | None, calc_claimed: Any | None, calc_blocked: str | None = None, manual_verification: bool = False
) -> RunDecision:
    """Unfixable blockers win (reject recommendation, a human still decides); fixable blockers -> needs_info;
    otherwise approve/partial from the calculation. Overridden findings are excluded."""
    active = [f for f in findings if not f.overridden and f.severity.value == "blocker"]
    unfixable = [f for f in active if not f.fixable]
    fixable = [f for f in active if f.fixable]
    if unfixable:
        return RunDecision("ready_for_decision", "reject", sorted({f.code for f in unfixable}), fixable, unfixable, manual_verification)
    if fixable:
        return RunDecision("needs_info", None, sorted({f.code for f in fixable}), fixable, [], manual_verification)
    if calc_blocked:
        return RunDecision("ready_for_decision", "reject", [f"calc.blocked.{calc_blocked.lower()}"], [], [], manual_verification)
    if calc_payable is None or calc_claimed is None:  # no calculation available -> manual verification
        return RunDecision("ready_for_decision", None, [], [], [], True)
    rec = "approve" if calc_payable == calc_claimed else "partial"
    return RunDecision("ready_for_decision", rec, [], [], [], manual_verification)
