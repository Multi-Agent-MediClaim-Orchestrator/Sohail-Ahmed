"""Money primitives (07 §6.1). Decimal only — no float anywhere in this package.

Ratios and per-line shares are kept as exact ``Fraction`` values and rounded **only** when a total is split
among lines (``allocate_cents``) or a single percentage amount is produced (``q``)."""

from __future__ import annotations

import decimal
from collections.abc import Mapping
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from fractions import Fraction

from .errors import EngineInvariantError

decimal.getcontext().prec = 28
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
Number = Decimal | Fraction | int


def frac(x: Number) -> Fraction:
    if isinstance(x, Fraction):
        return x
    if isinstance(x, Decimal):
        return Fraction(x)
    return Fraction(x)


def q(x: Number) -> Decimal:
    """Quantise to cents, half-up. Exact for Fractions (no float intermediate)."""
    if isinstance(x, Decimal):
        return x.quantize(CENT, rounding=ROUND_HALF_UP)
    f = frac(x) * 100
    cents = _round_half_up(f)
    return Decimal(cents) / 100


def _round_half_up(f: Fraction) -> int:
    """Round a non-negative-or-negative Fraction to int, ties away from zero (half-up for money)."""
    sign = -1 if f < 0 else 1
    f = abs(f)
    n, d = f.numerator, f.denominator
    return sign * ((2 * n + d) // (2 * d))


def pct(total: Number, percent: Number) -> Decimal:
    """``total * percent / 100`` rounded half-up once."""
    return q(frac(total) * frac(percent) / 100)


def cents(x: Decimal) -> int:
    return int((x * 100).to_integral_value())


def allocate_cents(total: Number, exact: Mapping[str, Number]) -> dict[str, Decimal]:
    """Largest-remainder allocation: per-line cent amounts that sum exactly to ``q(total)``.

    ``exact`` holds the unrounded per-line shares (their sum ≈ total). Ties on the fractional remainder are
    broken by ``line_ref`` ascending, so the result is deterministic and permutation invariant."""
    if not exact:
        return {}
    target = cents(q(total))
    shares = {k: frac(v) * 100 for k, v in exact.items()}
    floors = {k: (v.numerator // v.denominator) for k, v in shares.items()}
    short = target - sum(floors.values())
    n = len(exact)
    if short < 0 or short > n:
        raise EngineInvariantError(f"allocation_drift: short={short} n={n}")
    order = sorted(exact, key=lambda k: (-(shares[k] - floors[k]), k))
    for k in order[:short]:
        floors[k] += 1
    return {k: Decimal(c) / 100 for k, c in floors.items()}


def floor_cents(x: Fraction) -> Decimal:
    return Decimal((x * 100).numerator // (x * 100).denominator) / 100


__all__ = ["CENT", "ZERO", "ROUND_FLOOR", "allocate_cents", "q", "pct", "frac", "cents"]
