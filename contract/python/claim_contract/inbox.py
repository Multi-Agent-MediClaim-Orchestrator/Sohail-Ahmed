"""Sequence handling for at-least-once callbacks (01-01 section 9.2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SeqResult(StrEnum):
    APPLY = "apply"
    IGNORE = "ignore"  # duplicate / out-of-date: return stored success
    APPLY_GAP = "apply_gap"  # apply, then reconcile via GET status


@dataclass(frozen=True)
class SeqDecision:
    result: SeqResult
    new_last: int


def apply_sequence(last_applied: int, incoming: int) -> SeqDecision:
    if incoming <= last_applied:
        return SeqDecision(SeqResult.IGNORE, last_applied)
    if incoming > last_applied + 1:
        return SeqDecision(SeqResult.APPLY_GAP, incoming)
    return SeqDecision(SeqResult.APPLY, incoming)
