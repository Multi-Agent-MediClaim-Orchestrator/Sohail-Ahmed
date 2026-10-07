"""Deterministic claim assembly (doc 09 §6.5). Arithmetic is Decimal and never produced by a model; a model is
consulted (by the caller) only for line descriptions the keyword table cannot categorise."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from crew.tools import categories
from crew.tools.numbers import money, norm_text, parse_amount, parse_date

BILL_TYPES = ["final_bill", "procedure_bill", "itemised_bill", "pharmacy_bill", "lab_bill"]
DEFAULT_DESC = {
    "pharmacy_bill": "Pharmacy items",
    "final_bill": "Hospital charges",
    "procedure_bill": "Procedure charges",
}


def fv(v: Any) -> Any:
    """Typed values may be plain or {'value': ...}."""
    return v.get("value") if isinstance(v, dict) and "value" in v else v


def _line(raw: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any] | None:
    amount = parse_amount(fv(raw.get("amount")))
    if amount is None or amount < 0:
        return None
    desc = str(fv(raw.get("description")) or DEFAULT_DESC.get(doc["doc_type"], "Charges")).strip()[
        :200
    ]
    if len(desc) < 2:
        desc = DEFAULT_DESC.get(doc["doc_type"], "Charges")
    qty = parse_amount(fv(raw.get("qty"))) or Decimal(1)
    unit = parse_amount(fv(raw.get("unit_price")))
    if unit is None or (unit * qty).quantize(Decimal("0.01")) != amount.quantize(Decimal("0.01")):
        unit, qty = amount, Decimal(1)  # never invent a unit price that does not multiply out
    d = parse_date(fv(raw.get("date")))
    return {"description": desc, "qty": qty, "unit_price": unit, "amount": amount, "code": fv(raw.get("code")),
            "service_date": d, "doc": doc, "category": fv(raw.get("category"))}  # fmt: skip


def assemble(ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx = build-context from the API. Returns {payload, provenance, ambiguous, conflicts}."""
    case = ctx["case"]
    docs = ctx["documents"]
    bills = sorted(
        (d for d in docs if d["doc_type"] in BILL_TYPES),
        key=lambda d: BILL_TYPES.index(d["doc_type"]),
    )
    raw_lines: list[dict[str, Any]] = []
    discounts = Decimal(0)
    for b in bills:
        t = b.get("typed") or {}
        got = [
            x for x in (_line(r, b) for r in (fv(t.get("lines")) or []) if isinstance(r, dict)) if x
        ]
        if not got:
            total = parse_amount(fv(t.get("total")))
            if total is not None and total > 0:
                got = [x for x in [_line({"amount": str(total)}, b)] if x]
        raw_lines += got
        d = parse_amount(fv(t.get("discounts")))
        if d is not None and d > 0:
            discounts += d
    seen: set[tuple[Any, ...]] = set()
    lines: list[dict[str, Any]] = []
    for ln in raw_lines:  # precedence order already applied: the first bill that has a line wins
        key = (norm_text(ln["description"]), ln["service_date"], ln["amount"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(ln)
    ambiguous: list[int] = []
    for i, ln in enumerate(lines):
        cat = ln["category"] or categories.lookup(ln["description"])
        if cat is None and ln["doc"]["doc_type"] == "pharmacy_bill":
            cat = "medicine"
        if cat is None:
            ambiguous.append(i)
        ln["category"] = cat or "other"
    gross = sum((ln["amount"] for ln in lines), Decimal(0))
    discounts = min(discounts, gross)
    adm = case["admission"]
    pat = case["patient"]
    payload = {
        "patient": {
            k: pat[k] for k in ("full_name", "dob", "gender", "member_id", "policy_number")
        },
        "admission": {
            k: adm.get(k)
            for k in (
                "admission_type",
                "admitted_on",
                "discharged_on",
                "diagnosis_codes",
                "procedure_codes",
                "treating_doctor",
                "hospital_id",
                "preauth_ref",
            )
            if adm.get(k) is not None
        },  # fmt: skip
        "bill_lines": [
            {
                "line_no": i + 1,
                "code": ln["code"],
                "description": ln["description"],
                "category": ln["category"],
                "qty": format(ln["qty"], "f"),
                "unit_price": money(ln["unit_price"]),
                "amount": money(ln["amount"]),
                "service_date": ln["service_date"].isoformat() if ln["service_date"] else None,
                "source_doc_id": ln["doc"]["id"],
                "source_page": 1,
            }  # fmt: skip
            for i, ln in enumerate(lines)
        ],
        "totals": {
            "gross": money(gross),
            "discounts": money(discounts),
            "claimed": money(gross - discounts),
        },
        "documents": [d["id"] for d in docs],
    }
    for ln in payload["bill_lines"]:
        if ln["code"] is None:
            del ln["code"]
        if ln["service_date"] is None:
            del ln["service_date"]
    prov = {
        f"bill_lines[{i}].amount": {"doc_id": ln["doc"]["id"], "page": 1, "confidence": 0.9}
        for i, ln in enumerate(lines)
    }
    return {
        "payload": payload,
        "provenance": prov,
        "ambiguous": ambiguous,
        "lines": [ln["description"] for ln in lines],
    }


def recompute_totals(payload: dict[str, Any]) -> dict[str, Any]:
    p = {**payload, "totals": dict(payload["totals"])}
    gross = sum((parse_amount(ln["amount"]) or Decimal(0) for ln in p["bill_lines"]), Decimal(0))
    disc = min(parse_amount(p["totals"]["discounts"]) or Decimal(0), gross)
    p["totals"] = {"gross": money(gross), "discounts": money(disc), "claimed": money(gross - disc)}
    return p


ALLOWED_TOP = {"notes"}


def diff_paths(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """Changed paths, with line indexes written as [*] so they can be compared to repair_paths.yaml."""
    out: set[str] = set()

    def walk(a: Any, b: Any, path: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a.keys() | b.keys():
                walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for x, y in zip(a, b, strict=True):
                walk(x, y, f"{path}[*]")
        elif a != b:
            out.add(path)

    walk(before, after, "")
    return out
