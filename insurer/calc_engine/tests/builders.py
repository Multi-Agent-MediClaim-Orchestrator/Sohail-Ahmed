"""Input builders shared by golden generation, unit tests and property tests (JSON-shaped dicts)."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any

CASE_ID = "0191f0a2-0000-7000-8000-000000000001"
DOC_ID = "0191f0a2-0000-7000-8000-0000000000d1"
ADMITTED = date(2026, 9, 1)


def rules(**ov: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "order_profile": "standard",
        "room_rent": {"type": "percent_of_si", "percent": "1.0", "icu_percent": "2.0", "per_day_cap": None,
                      "tier_caps": {"A": "7000", "B": "5000", "C": "4000"}, "use_tier_caps": False},
        "proportionate_deduction": True,
        "proportionate_applies_to": ["doctor_fees", "surgeon_fees", "anaesthesia", "ot_charges", "nursing", "investigation", "procedure_package"],
        "proportionate_exempt": ["medicine", "implant", "consumable"],
        "sub_limits": {},
        "line_caps": {},
        "hospitalisation_windows": {"pre_days": 30, "post_days": 60},
        "co_pay": {"percent": "10", "conditions": {"age_gte": 61}},
        "non_network_co_pay_percent": "20",
        "stack_co_pay": False,
        "co_pay_order": "after_deductible",
        "deductible": {"amount": "0", "type": "per_claim"},
        "waiting_periods_days": {"initial": 30, "pre_existing": 730, "specific": {}},
        "pre_existing_group_map": {},
        "accident_icd_prefixes": ["S", "T"],
        "exclusions": {"icd_prefixes": [], "tags": ["cosmetic", "dental_cosmetic", "non_medical"], "non_medical_policy": "exclude_all"},
        "day_care_groups": ["cataract"],
        "sum_insured_basis": "individual",
    }
    for k, v in ov.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def line(ref: str, group: str, claimed: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "line_ref": ref, "category": kw.pop("category", "other"), "mapped_group": group,
        "description": kw.pop("description", f"{group} {ref}"), "qty": kw.pop("qty", "1"),
        "unit_price": kw.pop("unit_price", claimed), "claimed_amount": claimed, "mapping_source": "rule",
        "source_doc_id": DOC_ID,
    }
    d.update(kw)
    return d


def make_input(lines: list[dict[str, Any]], *, rules_ov: dict[str, Any] | None = None, admitted: date = ADMITTED,
               days: int = 3, adm_type: str = "planned", dx: list[str] | None = None, procedure_group: str | None = None,
               si: str = "500000.00", bonus: str = "0.00", utilised: str = "0.00", dob: str = "1981-04-02",
               cover_start: str = "2020-01-01", network: str = "network", status: str = "active",
               premium_until: str | None = None, pre_existing: list[str] | None = None, day_care: bool = False,
               claim_type: str = "cashless", declared_total: str | None = None, grace: int = 30) -> dict[str, Any]:
    out = {
        "case_id": CASE_ID, "claim_type": claim_type, "admission_type": adm_type,
        "policy": {"policy_number": "P-1", "product_code": "HF-GOLD", "status": status, "start_date": "2026-01-01",
                   "end_date": "2026-12-31", "premium_paid_until": premium_until or "2026-12-31", "grace_days": grace,
                   "sum_insured": si, "bonus_sum": bonus, "utilised_this_year": utilised, "sum_insured_basis": "individual"},
        "member": {"member_id": "M-9", "relationship": "self", "dob": dob, "cover_start": cover_start,
                   "pre_existing": [{"icd_prefix": p} for p in (pre_existing or [])]},
        "admission": {"admitted_on": admitted.isoformat(), "discharged_on": (admitted + timedelta(days=days)).isoformat(),
                      "admission_type": adm_type, "diagnosis_codes": dx or ["K35.8"], "procedure_codes": [],
                      "procedure_group": procedure_group, "day_care": day_care,
                      "hospital": {"hospital_id": "H-1", "network_status": network, "room_rent_tier": None}},
        "lines": lines, "rules": rules(**(rules_ov or {})), "rules_version": 12,
    }
    if declared_total is not None:
        out["declared_total"] = declared_total
    return copy.deepcopy(out)
