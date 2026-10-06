"""Deterministic needs-info messages per (doc_type, reason). The crew may polish them later (doc 09)."""

from __future__ import annotations

LABEL = {
    "prescription": "prescription",
    "pharmacy_bill": "pharmacy bill",
    "final_bill": "final bill",
    "itemised_bill": "itemised bill",
    "procedure_bill": "procedure bill",
    "implant_sticker": "implant sticker",
    "discharge_summary": "discharge summary",
    "lab_report": "lab report",
    "radiology_report": "radiology report",
    "claim_form": "claim form",
    "id_proof": "ID proof",
    "policy_card": "policy card",
    "admission_note": "admission note",
    "investigation_report": "investigation report",
    "preauth_approval": "pre-authorisation approval",
    "cancelled_cheque": "cancelled cheque",
    "payment_receipt": "payment receipt",
    "fir_mlc": "FIR / MLC copy",
}

TEMPLATES = {
    "not_uploaded": "A {label} is required but has not been uploaded. Please upload it.",
    "file_rejected": "The uploaded {label} was rejected by the virus scan. Please upload a clean copy.",
    "blurry": "The {label} is too blurry to read. Please re-scan it and upload again.",
    "cropped": "The {label} is cropped. Please re-scan the full page and upload again.",
    "unreadable": "The {label} cannot be read. Please upload a clearer copy.",
    "black_page": "A page of the {label} is black. Please re-scan it and upload again.",
    "wrong_orientation_unfixable": "The {label} is rotated and could not be corrected. Please re-scan it upright.",
    "stamp_missing": "The {label} has no hospital stamp. Please upload the stamped copy.",
    "stamp_low_confidence": "The hospital stamp on the {label} could not be confirmed. Please check the stamp.",
    "stamp_not_evaluated": "The stamp on the {label} could not be checked in time. Please review it manually.",
    "low_parse_confidence": "The {label} could not be read reliably. Please review its details.",
    "passes_disagree": "The two reading passes disagree on the {label}. Please review its details.",
    "classification_low_confidence": "The document type could not be determined. Please confirm what this document is.",
    "bill_total_mismatch": "The line items of the {label} do not add up to its total. Please check the bill.",
    "not_chronological": "A bill is dated before the prescription. Please check the dates or the order.",
    "patient_name_mismatch": "The patient name differs between documents. Please check the {label}.",
    "date_out_of_window": "The {label} is dated outside the admission period. Please check it.",
    "policy_number_mismatch": "The policy number on the {label} differs from the declared one.",
    "dob_mismatch": "The date of birth differs between documents. Please check them.",
    "admission_dates_mismatch": "The admission/discharge dates on the {label} differ from the case.",
    "hospital_name_mismatch": "The hospital name differs between stamped documents. Please check them.",
}


def label(doc_type: str | None) -> str:
    return LABEL.get(doc_type or "", (doc_type or "document").replace("_", " "))


def render(doc_type: str | None, reason: str) -> str:
    base, _, field = reason.partition(":")
    if base == "field_missing":
        return f"The {label(doc_type)} is missing {field.replace('_', ' ')}. Please upload a complete copy."
    return TEMPLATES.get(base, "Please review the {label}.").format(label=label(doc_type))
