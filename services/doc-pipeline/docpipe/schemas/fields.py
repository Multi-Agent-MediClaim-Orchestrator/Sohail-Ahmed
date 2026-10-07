"""Extraction schemas: field -> (kind, critical). Typed keys match what hospital-api reads (patient_name, date, total,
admitted_on, discharged_on, lines). Critical = money/date/identifier: they trigger pass B and strict gating."""

from __future__ import annotations

FIELDS: dict[str, dict[str, tuple[str, bool]]] = {
    "final_bill": {
        "bill_number": ("text", False),
        "date": ("date", True),
        "patient_name": ("text", False),
        "admitted_on": ("date", True),
        "discharged_on": ("date", True),
        "total": ("money", True),
        "discounts": ("money", True),
        "hospital_name": ("text", False),
    },
    "itemised_bill": {
        "bill_number": ("text", False),
        "date": ("date", True),
        "patient_name": ("text", False),
        "total": ("money", True),
    },
    "pharmacy_bill": {
        "bill_number": ("text", False),
        "date": ("date", True),
        "patient_name": ("text", False),
        "total": ("money", True),
    },
    "procedure_bill": {
        "bill_number": ("text", False),
        "date": ("date", True),
        "patient_name": ("text", False),
        "total": ("money", True),
    },
    "prescription": {
        "patient_name": ("text", False),
        "date": ("date", True),
        "doctor_name": ("text", False),
        "medicines": ("list", False),
    },
    "discharge_summary": {
        "patient_name": ("text", False),
        "admitted_on": ("date", True),
        "discharged_on": ("date", True),
        "diagnosis": ("text", False),
        "icd_codes": ("list", False),
        "doctor_name": ("text", False),
    },
    "admission_note": {
        "patient_name": ("text", False),
        "admitted_on": ("date", True),
        "provisional_diagnosis": ("text", False),
    },
    "lab_report": {
        "patient_name": ("text", False),
        "date": ("date", True),
        "lab_name": ("text", False),
    },
    "radiology_report": {
        "patient_name": ("text", False),
        "date": ("date", True),
        "impression": ("text", False),
    },
    "investigation_report": {"patient_name": ("text", False), "date": ("date", True)},
    "preauth_approval": {
        "preauth_ref": ("id", True),
        "approved_amount": ("money", True),
        "valid_till": ("date", True),
        "insurer_name": ("text", False),
    },
    "claim_form": {
        "policy_number": ("id", True),
        "member_id": ("id", True),
        "patient_name": ("text", False),
        "claim_amount": ("money", True),
    },
    "policy_card": {
        "policy_number": ("id", True),
        "member_id": ("id", True),
        "insurer_name": ("text", False),
        "valid_from": ("date", True),
        "valid_to": ("date", True),
        "product_name": ("text", False),  # policy terms for the hospital's admissible-amount estimate
        "sum_insured": ("money", False),
    },
    "id_proof": {"id_type": ("text", False), "name": ("text", False), "dob": ("date", True)},
    "cancelled_cheque": {
        "account_holder": ("text", False),
        "ifsc": ("id", True),
        "bank_name": ("text", False),
    },
    "implant_sticker": {
        "implant_name": ("text", False),
        "manufacturer": ("text", False),
        "serial_no": ("id", True),
        "mrp": ("money", True),
    },
    "payment_receipt": {
        "receipt_no": ("text", False),
        "date": ("date", True),
        "amount": ("money", True),
    },
    "fir_mlc": {
        "mlc_no": ("text", False),
        "date": ("date", True),
        "police_station": ("text", False),
    },
    "other": {"title": ("text", False)},
}
LOCAL_ONLY = {"id_proof", "cancelled_cheque"}  # never a cloud model, not even on masked text
LINE_DOCS = {"final_bill", "itemised_bill", "pharmacy_bill", "procedure_bill"}
HIGH_VALUE = {
    "final_bill",
    "claim_form",
    "preauth_approval",
    "procedure_bill",
    "itemised_bill",
    "pharmacy_bill",
}
