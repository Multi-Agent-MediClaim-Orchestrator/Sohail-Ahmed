"""v1 payloads for the hospital config domains (01-03 §4.1 and docs 03-07)."""

DOC_REQUIREMENTS = {
    "rules": [
        {
            "id": "R-RX-01",
            "doc_type": "prescription",
            "applies": {"claim_type": ["*"]},
            "requirement": "required",
            "min_parse_confidence": 0.80,
            "must_have_fields": ["patient_name", "date", "medicines"],
        },
        {
            "id": "R-PH-01",
            "doc_type": "pharmacy_bill",
            "applies": {"claim_type": ["*"]},
            "requirement": "required",
            "must_have_stamp": True,
            "must_have_fields": ["lines", "total", "date"],
        },
        {
            "id": "R-BILL-01",
            "doc_type": "final_bill",
            "applies": {"claim_type": ["*"]},
            "requirement": "required",
            "must_have_stamp": True,
            "must_have_fields": ["lines", "total"],
        },
        {
            "id": "R-PROC-01",
            "doc_type": "procedure_bill",
            "applies": {"flags": ["has_surgery"]},
            "requirement": "conditional",
            "must_have_stamp": True,
        },
        {
            "id": "R-IMP-02",
            "doc_type": "implant_sticker",
            "applies": {"procedure_group": ["ortho_implant", "cardiac_stent"]},
            "requirement": "conditional",
        },
    ],
    "alternatives": [{"any_of": ["final_bill", "itemised_bill"], "for": "R-BILL-01-alt"}],
    "ordering": {
        "chronological": True,
        "by": "document_date",
        "rule_id": "R-ORD-01",
        "severity": "warning",
    },
    "procedure_groups": {"ortho_implant": ["0SR9*", "0SRB*"], "cardiac_stent": ["02703*"]},
    "stamp_rules": {
        k: {"min_stamp_confidence": 0.7}
        for k in ("pharmacy_bill", "final_bill", "procedure_bill", "itemised_bill")
    },
}

DEADLINES = {
    "reminder_offsets_hours": [24, 48, 72],
    "reimbursement_filing_days": 30,
    "query_response_sla_hours": 72,
    "request_sla_hours": 72,
    "intimation_emergency_hours": 24,
    "planned_preauth_lead_hours": 48,
}

ROUTER_RULES = {
    "claim_type_rules": [
        {"if": {"admission_source": "ER", "hospital_network": True}, "then": "cashless"},
        {"if": {"preauth_ref_present": True, "hospital_network": True}, "then": "cashless"},
        {"if": {"default": True}, "then": "reimbursement"},
    ],
    "emergency_rules": {
        "intimation_hours": 24,
        "signals": ["admission_note.contains_emergency", "admission_source=ER"],
    },
    "flag_rules": [
        {"flag": "implant", "if": {"procedure_group": ["ortho_implant", "cardiac_stent"]}},
        {"flag": "high_value", "if": {"claimed_amount_gte": 500000}},
        {
            "flag": "medico_legal",
            "if": {"icd10_prefix": ["S", "T", "X", "Y"], "or_doc_type_present": ["fir_mlc"]},
        },
        {"flag": "day_care", "if": {"stay_days_lte": 0}},
        {"flag": "maternity", "if": {"icd10_prefix": ["O"]}},
    ],
    "step_templates": {
        "cashless": ["preauth_check", "completeness", "claim_build", "officer_signoff", "submit"],
        "reimbursement": [
            "completeness",
            "claim_build",
            "officer_signoff",
            "submit",
            "filing_window_check",
        ],
    },
}

CONFIDENCE_GATES = {
    "parse_min": 0.75,
    "classification_min": 0.80,
    "agreement_min": 0.90,
    "quality_min": 0.5,
    "amount_tolerance_inr": 1.00,
}
SIGNOFF = {"four_eyes": False, "ack_required": True}
QUERY_POLICY = {"max_rounds": 3, "default_sla_hours": 72, "hospital_round3_two_person": True}

DOMAINS = {
    "doc_requirements": DOC_REQUIREMENTS,
    "deadlines": DEADLINES,
    "router_rules": ROUTER_RULES,
    "confidence_gates": CONFIDENCE_GATES,
    "signoff": SIGNOFF,
    "query_policy": QUERY_POLICY,
}
