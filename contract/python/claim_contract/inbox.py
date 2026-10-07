"""Sequence handling for at-least-once callbacks (01-01 section 9.2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SeqResult(StrEnum):
    APPLY = "apply"
    IGNORE = "ignore"  # duplicate / out-of-date: return stored success
    APPLY_GAP = "apply_gap"  # apply, then reconcile via GET status


class SequenceAction(StrEnum):
    apply = "apply"
    ignore = "ignore"


@dataclass(frozen=True)
class SeqDecision:
    result: SeqResult
    new_last: int

    # Dev B's receiver code reads these views of the same decision
    @property
    def action(self) -> SequenceAction:
        return SequenceAction.ignore if self.result is SeqResult.IGNORE else SequenceAction.apply

    @property
    def gap_detected(self) -> bool:
        return self.result is SeqResult.APPLY_GAP

    @property
    def expected(self) -> int:
        return self.new_last + 1 if self.result is SeqResult.IGNORE else self.new_last


def apply_sequence(last_applied: int, incoming: int) -> SeqDecision:
    if incoming <= last_applied:
        return SeqDecision(SeqResult.IGNORE, last_applied)
    if incoming > last_applied + 1:
        return SeqDecision(SeqResult.APPLY_GAP, incoming)
    return SeqDecision(SeqResult.APPLY, incoming)
