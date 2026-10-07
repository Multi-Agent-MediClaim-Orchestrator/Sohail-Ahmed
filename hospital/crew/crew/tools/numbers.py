"""Number and date normalisers (Indian formats): lakh commas, Rs./₹ prefixes, trailing /-, '1.2L'."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

_CLEAN = re.compile(r"(?i)(rs\.?|inr|₹|/-|\s)")


def parse_amount(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, Decimal)):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))
    s = _CLEAN.sub("", str(raw)).replace(",", "")
    mult = Decimal(1)
    if s[-1:].lower() == "l":
        mult, s = Decimal(100000), s[:-1]
    elif s[-1:].lower() == "k":
        mult, s = Decimal(1000), s[:-1]
    try:
        return Decimal(s) * mult
    except InvalidOperation:
        return None


def money(d: Decimal) -> str:
    return format(d.quantize(Decimal("0.01")), "f")


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub("[​‌‍﻿]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(raw: object) -> date | None:
    """ISO first, then day-first (India): 28/09/2026, 28-09-26."""
    if isinstance(raw, date):
        return raw
    s = str(raw or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})", s)
        if m:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None
