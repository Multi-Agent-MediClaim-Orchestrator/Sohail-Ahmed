"""Fake identifiers that validate but cannot collide with issued ones: Aadhaar-like numbers start with 9999 and carry a
valid Verhoeff check digit; PAN-like start with ZZZ; IFSC use the FAKE0 prefix; accounts start with 9999."""

from __future__ import annotations

import random
import re

_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
      [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]  # fmt: skip
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
      [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]  # fmt: skip


def verhoeff_valid(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(re.sub(r"\D", "", num))):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def with_check_digit(prefix: str) -> str:
    for d in range(10):
        if verhoeff_valid(prefix + str(d)):
            return prefix + str(d)
    raise AssertionError("unreachable")


def fake_aadhaar(rng: random.Random) -> str:
    return with_check_digit("9999" + "".join(str(rng.randrange(10)) for _ in range(7)))


def fake_pan(rng: random.Random) -> str:
    return (
        "ZZZ"
        + rng.choice("ABCDEFGHJKLMNPQRSTUVWXY")
        + rng.choice("ABCDEFGHJKLMNPQRSTUVWXY")
        + f"{rng.randrange(10000):04d}"
        + rng.choice("ABCDEFGHJKLMNPQRSTUVWXY")
    )


def fake_phone(rng: random.Random) -> str:
    return "9" + "".join(
        str(rng.randrange(10)) for _ in range(9)
    )  # clearly fictional range is impossible; never used outside fixtures


def fake_ifsc(rng: random.Random) -> str:
    return "FAKE0" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(6))


def fake_account(rng: random.Random) -> str:
    return "9999" + "".join(str(rng.randrange(10)) for _ in range(8))


REAL_LOOKING_AADHAAR = re.compile(
    r"(?<![0-9A-Za-z])(?!9999)[2-9]\d{3}\s?\d{4}\s?\d{4}(?![0-9A-Za-z])"
)  # not inside hex hashes or ids


def real_looking_aadhaar(text: str) -> list[str]:
    """Verhoeff-valid 12-digit numbers that are NOT in the reserved 9999 range (the lint fails on any)."""
    return [m for m in REAL_LOOKING_AADHAAR.findall(text) if verhoeff_valid(m)]
