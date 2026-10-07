"""Hospital-side scenario archetypes (doc 05-01 §6). A recipe says which documents exist and which defects apply."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Recipe:
    id: str
    name: str
    claim_type: str = "cashless"
    admission_type: str = "planned"
    procedure: str | None = None  # a specific procedure code, else random non-implant
    drop: tuple[str, ...] = ()  # documents that are missing
    degrade: dict[str, dict[str, Any]] = field(default_factory=dict)  # doc_type -> degrade params
    no_stamp: tuple[str, ...] = ()
    total_mismatch: tuple[str, ...] = ()
    pharmacy_lines: int = 12
    pii_text: bool = False
    weight: float = 1.0


RECIPES: dict[str, Recipe] = {r.id: r for r in [
    Recipe("S01", "clean_cashless_planned", weight=4),
    Recipe("S02", "clean_cashless_emergency", admission_type="emergency", procedure="pneumonia", weight=3),
    Recipe("S03", "clean_reimbursement_planned", claim_type="reimbursement", weight=3),
    Recipe("S05", "missing_required_doc", drop=("final_bill",), weight=2),
    Recipe("S06", "missing_conditional_doc", procedure="knee_replacement", drop=("implant_sticker",), weight=2),
    Recipe("S07", "blurry_final_bill", degrade={"final_bill": {"blur": 3.0}}, weight=2),
    Recipe("S08", "missing_stamp", no_stamp=("final_bill",), weight=2),
    Recipe("S10", "bill_total_mismatch", total_mismatch=("pharmacy_bill",), weight=2),
    Recipe("S23", "low_resolution_skew", degrade={"pharmacy_bill": {"dpi": 80, "skew": 4}}, weight=1),
    Recipe("S24", "multi_page_bill", pharmacy_lines=70, weight=1),
    Recipe("S25", "pii_stress", pii_text=True, weight=1),
]}  # fmt: skip
