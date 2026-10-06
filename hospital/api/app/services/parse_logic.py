"""Deterministic parse gates (doc 03 tasks 8-9): classification gate and two-pass agreement."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz

DOC_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "prescription": ("patient_name", "date"),
    "pharmacy_bill": ("total", "date"),
    "final_bill": ("total", "date"),
    "itemised_bill": ("total", "date"),
    "procedure_bill": ("total", "date"),
    "discharge_summary": ("patient_name", "admitted_on", "discharged_on"),
    "lab_report": ("patient_name", "date"),
    "radiology_report": ("patient_name", "date"),
}
DEFAULT_KEYS = ("patient_name", "date", "total")
NAME_KEYS = {"patient_name", "name", "doctor_name"}


def classification_gate(confidence: float | None, minimum: float) -> bool:
    return confidence is not None and confidence >= minimum


def norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return Decimal(str(v)).quantize(Decimal("0.01"))
    s = unicodedata.normalize("NFKC", str(v)).strip().lower()
    cleaned = re.sub(r"[₹,\s]|rs\.?|inr|/-", "", s)
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))  # money-like strings compare as Decimal
    except InvalidOperation:
        return re.sub(r"\s+", " ", s)


def _name_ratio(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    return float(fuzz.token_set_ratio(str(a), str(b))) / 100.0


def agreement(
    p1: dict[str, Any], p2: dict[str, Any], doc_type: str | None, name_min: int = 90
) -> float:
    keys = DOC_KEY_FIELDS.get(doc_type or "", DEFAULT_KEYS)
    keys = tuple(k for k in keys if k in p1 or k in p2)  # only fields at least one pass produced
    if not keys:
        return 0.0
    scores = []
    for k in keys:
        a, b = norm(p1.get(k)), norm(p2.get(k))
        if a is None and b is None:
            scores.append(0.0)
        elif a == b:
            scores.append(1.0)
        elif k in NAME_KEYS:
            r = _name_ratio(a, b)
            scores.append(r if r * 100 >= name_min else 0.0)
        else:
            scores.append(0.0)
    return sum(scores) / len(scores)


def disagreeing_fields(p1: dict[str, Any], p2: dict[str, Any], doc_type: str | None) -> list[str]:
    keys = DOC_KEY_FIELDS.get(doc_type or "", DEFAULT_KEYS)
    return [
        k
        for k in keys
        if (k in p1 or k in p2)
        and norm(p1.get(k)) != norm(p2.get(k))
        and not (k in NAME_KEYS and _name_ratio(norm(p1.get(k)), norm(p2.get(k))) >= 0.9)
    ]


def derive_status(
    *,
    scan_clean: bool,
    active: bool,
    passes: int,
    doc_type: str | None,
    agreement_score: float | None,
    parse_confidence: float | None,
    gates: dict[str, Any],
) -> str | None:
    """New parse_status, or None to leave unchanged."""
    if not scan_clean or not active:
        return None
    if passes < 2:
        return "processing"
    if (
        doc_type is None
        or agreement_score is None
        or agreement_score < gates["agreement_min"]
        or parse_confidence is None
        or parse_confidence < gates["parse_min"]
    ):
        return "needs_review"
    return "parsed"
