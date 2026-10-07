"""Default (seed) config payloads. PROPOSED values; real values are rows edited through the admin workflow."""

from __future__ import annotations

from typing import Any

PRODUCTS: dict[str, dict[str, Any]] = {
    "HEALTH-BASIC": {"name": "Health Basic", "si": ["300000", "500000"], "room_pct": "1.0", "icu_pct": "2.0", "copay": "10", "copay_age": 61},
    "HEALTH-PLUS-GOLD": {"name": "Health Plus Gold", "si": ["500000", "1000000"], "room_pct": "1.0", "icu_pct": "2.0", "copay": "0", "copay_age": None},
    "SENIOR-SHIELD": {"name": "Senior Shield", "si": ["500000", "1000000", "2500000"], "room_pct": "1.5", "icu_pct": "3.0", "copay": "20", "copay_age": 0},
}


def policy_rules(product_code: str) -> dict[str, Any]:
    p = PRODUCTS[product_code]
    co_pay: dict[str, Any] = {"percent": p["copay"], "conditions": {} if p["copay_age"] is None else {"age_gte": p["copay_age"]}}
    return {
        "product_code": product_code,
        "rules": {
            "schema_version": 1,
            "order_profile": "standard",
            "room_rent": {"type": "percent_of_si", "percent": p["room_pct"], "icu_percent": p["icu_pct"], "per_day_cap": None,
                          "tier_caps": {"A": "7000", "B": "5000", "C": "4000"}, "use_tier_caps": False},
            "proportionate_deduction": True,
            "sub_limits": {"cataract": "40000", "knee_replacement": "150000", "maternity": "75000"},
            "line_caps": {"ambulance": "3000"},
            "hospitalisation_windows": {"pre_days": 30, "post_days": 60},
            "co_pay": co_pay,
            "non_network_co_pay_percent": "20",
            "stack_co_pay": False,
            "co_pay_order": "after_deductible",
            "deductible": {"amount": "0", "type": "per_claim"},
            "waiting_periods_days": {"initial": 30, "pre_existing": 730, "specific": {"cataract": 730, "hernia": 365, "knee_replacement": 730}},
            "pre_existing_group_map": {"E11": ["diabetes_management"], "I10": ["hypertension_management"]},
            "accident_icd_prefixes": ["S", "T", "V", "W", "X", "Y"],
            "exclusions": {"icd_prefixes": ["Z41", "N97"], "tags": ["cosmetic", "dental_cosmetic", "non_medical"], "non_medical_policy": "exclude_all"},
            "day_care_groups": ["cataract", "dialysis", "chemotherapy"],
            "sum_insured_basis": "floater",
        },
    }


THRESHOLDS: dict[str, Any] = {"t_auto_inr": "50000", "t_four_inr": "500000"}
QUERY_POLICY: dict[str, Any] = {}
DOC_REQUIREMENTS: dict[str, Any] = {
    "required": ["discharge_summary", "final_bill", "itemised_bill", "claim_form", "id_proof", "policy_card"],
    "optional": ["pharmacy_bill", "lab_report", "radiology_report", "investigation_report", "admission_note", "preauth_approval"],
    "conditional": [
        {"if": {"bill_has_category": "implant"}, "require": ["implant_sticker"]},
        {"if": {"claim_type": "reimbursement"}, "require": ["payment_receipt", "cancelled_cheque"]},
        {"if": {"admission_type": "emergency", "medico_legal": True}, "require": ["fir_mlc"]},
    ],
    "stamp_required_for": ["final_bill", "itemised_bill", "pharmacy_bill"],
    "min_parse_confidence": 0.80,
}
