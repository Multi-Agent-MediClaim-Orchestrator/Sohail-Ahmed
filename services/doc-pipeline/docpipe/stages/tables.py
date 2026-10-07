"""Deterministic bill-line extraction from layout text: `description  [qty]  [rate]  amount`. Table rows are code,
not model output (principle 1); the model only handles the header fields."""

from __future__ import annotations

import re
from decimal import Decimal

from docpipe.stages.numbers import parse_amount, parse_date

AMT = r"(?:Rs\.?|₹|INR)?\s*\d[\d,]*(?:\.\d{1,2})?(?:/-)?"
ROW = re.compile(
    rf"^\s*(?:\d{{1,3}}[.)]?\s+)?(?P<desc>[A-Za-z][^\n]*?[A-Za-z)\]])\s{{2,}}(?P<nums>(?:{AMT}\s*)+)$"
)
TOTAL = re.compile(
    r"(?i)\b(sub\s*total|grand\s*total|net\s*amount|total|balance|advance|discount|gst|tax)\b"
)


def extract_lines(text: str) -> tuple[list[dict[str, str]], Decimal | None, Decimal | None]:
    """Returns (lines, printed_total, printed_discount)."""
    lines: list[dict[str, str]] = []
    total: Decimal | None = None
    discount: Decimal | None = None
    for raw in text.splitlines():
        m = ROW.match(raw)
        if not m:
            continue
        desc = re.sub(r"\s+", " ", m["desc"]).strip(" .:-")
        nums = [parse_amount(x) for x in re.findall(AMT, m["nums"])]
        nums = [n for n in nums if n is not None]
        if not nums:
            continue
        if TOTAL.search(desc):
            low = desc.lower()
            if "discount" in low:
                discount = nums[-1]
            elif re.search(r"(?i)grand|net amount|^total|sub\s*total", desc):
                total = nums[-1]
            continue
        amount = nums[-1]
        ln = {"description": desc, "amount": format(amount, "f")}
        if len(nums) >= 3:
            ln["qty"], ln["unit_price"] = format(nums[0], "f"), format(nums[1], "f")
        elif len(nums) == 2:
            ln["unit_price"] = format(nums[0], "f")
        d = re.search(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b", raw)
        if d and parse_date(d.group(1)):
            ln["date"] = parse_date(d.group(1)).isoformat()  # type: ignore[union-attr]
        lines.append(ln)
    return lines, total, discount
