"""Mutable working copy used by the steps. Never exposed outside the package."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal

from .models import BlockedReason, CalcFlag, CalcLine, FlagCode, TraceStep
from .money import ZERO


@dataclass
class Hit:
    rule_id: str
    step: str
    amount: Decimal
    explanation: str


@dataclass
class LineState:
    line: CalcLine
    claimed: Decimal
    allowed: Decimal
    ded: Decimal = ZERO
    copay: Decimal = ZERO
    sicut: Decimal = ZERO
    hits: list[Hit] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return self.line.line_ref

    @property
    def net(self) -> Decimal:
        return self.allowed - self.ded - self.copay - self.sicut


@dataclass
class EngineState:
    lines: list[LineState]
    blocked: BlockedReason | None = None
    flags: list[CalcFlag] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    eligible_total: Decimal = ZERO
    remaining_before: Decimal = ZERO
    remaining_after: Decimal = ZERO
    copay_rule: str | None = None
    # which lines had which group blocked at S1 (for the S6 'all excluded' decision)

    @classmethod
    def from_lines(cls, lines: list[CalcLine]) -> EngineState:
        return cls([LineState(ln, ln.claimed_amount, ln.claimed_amount) for ln in lines])

    def clone(self) -> EngineState:
        return copy.deepcopy(self)

    def net_total(self) -> Decimal:
        return sum((ls.net for ls in self.lines), ZERO)

    def flag(self, code: FlagCode, message: str, line_ref: str | None = None) -> None:
        if not any(f.code == code and f.line_ref == line_ref and f.message == message for f in self.flags):
            self.flags.append(CalcFlag(code=code, message=message, line_ref=line_ref))

    def cut(self, ls: LineState, amount: Decimal, rule_id: str, step: str, why: str) -> None:
        """Reduce a line's allowed amount by ``amount`` (>0) and record the hit."""
        if amount <= 0:
            return
        if amount > ls.allowed:
            amount = ls.allowed  # clamp (never below zero); property tests assert this never triggers
        ls.allowed -= amount
        ls.hits.append(Hit(rule_id, step, amount, why[:500]))

    def disallow_line(self, ls: LineState, rule_id: str, step: str, why: str) -> None:
        self.cut(ls, ls.allowed, rule_id, step, why)

    def block(self, reason: BlockedReason, rule_id: str, step: str, why: str) -> None:
        self.blocked = reason
        for ls in self.lines:
            self.disallow_line(ls, rule_id, step, why)

    def record(self, step: str, description: str, before: Decimal, rule_id: str | None = None,
               affected: list[str] | None = None) -> None:
        self.trace.append(TraceStep(step=step, rule_id=rule_id, description=description, before_total=before,
                                    after_total=self.net_total(), affected_lines=sorted(set(affected or []))))
