"""S9: deterministic validation (Decimal only). Returns issues; never edits values."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from docpipe.stages.entities import verhoeff_ok
from docpipe.stages.numbers import parse_amount, parse_date

TOL = Decimal("0.01")


def validate(
    doc_type: str, typed: dict[str, Any], lines: list[dict[str, str]], printed_total: Decimal | None
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if lines:
        total = parse_amount(typed.get("total")) or printed_total
        s = sum((parse_amount(x["amount"]) or Decimal(0) for x in lines), Decimal(0))
        if total is not None and abs(s - total) > TOL:
            out.append(
                {
                    "code": "table_total_mismatch",
                    "field": "total",
                    "detail": f"lines sum to {s} but total is {total}",
                }
            )  # V01
        for i, ln in enumerate(lines):  # V04
            q, r, a = (
                parse_amount(ln.get("qty")),
                parse_amount(ln.get("unit_price")),
                parse_amount(ln["amount"]),
            )
            if q is not None and r is not None and a is not None and abs(q * r - a) > TOL:
                out.append(
                    {
                        "code": "line_arithmetic",
                        "field": f"lines[{i}]",
                        "detail": "qty x rate differs from amount",
                    }
                )
        seen: set[tuple[str, str]] = set()
        for ln in lines:  # V14
            k = (ln["description"].lower(), ln["amount"])
            if k in seen:
                out.append(
                    {
                        "code": "possible_duplicate_line",
                        "field": "lines",
                        "detail": ln["description"],
                    }
                )
            seen.add(k)
    for ak in ("total", "discounts", "claim_amount", "approved_amount", "amount", "mrp"):  # V07
        v = typed.get(ak)
        if v is not None:
            amt = parse_amount(v)
            if amt is None or amt < 0 or not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", format(amt, "f")):
                out.append({"code": "amount_format", "field": ak, "detail": "not a valid amount"})
    adm, dis = parse_date(typed.get("admitted_on")), parse_date(typed.get("discharged_on"))
    if adm and dis:  # V05
        if dis < adm:
            out.append(
                {
                    "code": "date_order",
                    "field": "discharged_on",
                    "detail": "discharge is before admission",
                }
            )
        elif (dis - adm).days > 365:
            out.append(
                {
                    "code": "date_order",
                    "field": "discharged_on",
                    "detail": "stay longer than a year",
                }
            )
    for dk in (
        "date",
        "admitted_on",
        "discharged_on",
        "dob",
        "valid_till",
        "valid_from",
        "valid_to",
    ):
        dv = parse_date(typed.get(dk))
        if dv and dv > date.today().replace(year=date.today().year + 2):
            out.append({"code": "date_invalid", "field": dk, "detail": "implausible date"})
    vf, vt = parse_date(typed.get("valid_from")), parse_date(typed.get("valid_to"))
    if vf and vt and vf >= vt:  # V12
        out.append(
            {
                "code": "date_order",
                "field": "valid_to",
                "detail": "valid_from is not before valid_to",
            }
        )
    if typed.get("ifsc") and not re.fullmatch(
        r"[A-Z]{4}0[A-Z0-9]{6}", str(typed["ifsc"]).upper()
    ):  # V11
        out.append({"code": "invalid_ifsc", "field": "ifsc", "detail": "bad IFSC"})
    for c in typed.get("icd_codes") or []:  # V08
        if not re.fullmatch(r"[A-TV-Z]\d{2}(\.\d{1,4})?", str(c).upper()):
            out.append({"code": "icd_format", "field": "icd_codes", "detail": str(c)})
    return out


def id_checks(doc_type: str, raw_text: str) -> list[dict[str, str]]:
    """V09/V10 run on the RAW text locally, before masking; only the verdict leaves this function."""
    out: list[dict[str, str]] = []
    if doc_type == "id_proof":
        for m in re.findall(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)", raw_text):
            if not verhoeff_ok(m):
                out.append(
                    {
                        "code": "invalid_id_checksum",
                        "field": "id_number",
                        "detail": "Aadhaar checksum failed",
                    }
                )
        for m in re.findall(r"\b[A-Z]{5}\d{4}[A-Z]\b", raw_text):
            if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", m):
                out.append({"code": "invalid_pan", "field": "id_number", "detail": "bad PAN"})
    return out
