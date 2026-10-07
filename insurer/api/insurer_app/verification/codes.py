"""Finding-code catalogue (03-03 §3). Unknown codes from agents are rejected (422) until added here."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeInfo:
    step: str
    severity: str  # default severity: info | warning | blocker
    fixable: bool
    category: str | None  # suggested QueryCategory when fixable
    doc_types: tuple[str, ...] = ()
    message: str = ""


def _c(step: str, sev: str, fixable: bool, cat: str | None = None, docs: tuple[str, ...] = (), msg: str = "") -> CodeInfo:
    return CodeInfo(step, sev, fixable, cat, docs, msg)


CODES: dict[str, CodeInfo] = {
    # document_fetch
    "doc.fetch_failed": _c("document_fetch", "blocker", True, "missing_document", msg="A document could not be downloaded"),
    "doc.hash_mismatch": _c("document_fetch", "blocker", True, "illegible_document", msg="Document content does not match its declared checksum"),
    "doc.virus_found": _c("document_fetch", "blocker", True, "illegible_document", msg="Document failed the malware scan"),
    "doc.url_expired": _c("document_fetch", "blocker", True, "missing_document", msg="Document link expired and could not be refreshed"),
    "doc.fetch_timeout": _c("document_fetch", "blocker", True, "missing_document", msg="Documents were not available in time"),
    # completeness
    "completeness.missing_required": _c("completeness", "blocker", True, "missing_document", msg="A required document is missing"),
    "completeness.low_parse_confidence": _c("completeness", "warning", True, "illegible_document", msg="Document could not be read reliably"),
    "completeness.wrong_doc_type": _c("completeness", "warning", True, "missing_document", msg="Document type does not match its content"),
    "completeness.unsigned_discharge": _c("completeness", "blocker", True, "missing_document", ("discharge_summary",), "Discharge summary is not signed"),
    # identity
    "identity.policy_not_found": _c("identity", "blocker", True, "identity_mismatch", ("policy_card", "id_proof"), "Policy number not found"),
    "identity.member_not_found": _c("identity", "blocker", True, "identity_mismatch", ("policy_card", "id_proof"), "Member not found on the policy"),
    "identity.name_mismatch": _c("identity", "warning", False, "identity_mismatch", ("id_proof",), "Patient name differs from the member record"),
    "identity.dob_mismatch": _c("identity", "blocker", True, "identity_mismatch", ("id_proof",), "Date of birth differs from the member record"),
    "identity.gender_mismatch": _c("identity", "warning", False, "identity_mismatch", ("id_proof",), "Gender differs from the member record"),
    "identity.id_hash_mismatch": _c("identity", "blocker", False, None, (), "ID proof does not match the member record"),
    "identity.low_score": _c("identity", "blocker", True, "identity_mismatch", ("id_proof", "policy_card"), "Identity match score is below the threshold"),
    # authenticity
    "auth.stamp_missing": _c("authenticity", "blocker", True, "missing_document", (), "Hospital stamp not found on a bill"),
    "auth.signature_missing": _c("authenticity", "blocker", True, "missing_document", (), "Signature not found on a document"),
    "auth.tamper_suspected": _c("authenticity", "blocker", False, None, (), "Document shows signs of alteration"),
    "auth.bill_arithmetic": _c("authenticity", "blocker", True, "billing_discrepancy", ("itemised_bill", "final_bill"), "Bill totals do not add up"),
    "auth.duplicate_claim": _c("authenticity", "blocker", False, None, (), "Overlapping claim for the same member and hospital"),
    "auth.duplicate_document": _c("authenticity", "blocker", False, None, (), "The same file was used on another member's claim"),
    "auth.dates_inconsistent": _c("authenticity", "warning", True, "medical_clarification", (), "Dates on documents are inconsistent with the stay"),
    "auth.hospital_not_empanelled": _c("authenticity", "blocker", False, None, (), "Hospital is not empanelled for cashless claims"),
    "auth.fraud_warning": _c("authenticity", "warning", False, None, (), "Authenticity score is below the warning floor"),
    # coverage
    "coverage.policy_inactive": _c("coverage", "blocker", False, None, (), "Policy is not active"),
    "coverage.policy_grace": _c("coverage", "warning", False, None, (), "Policy premium is in the grace period"),
    "coverage.outside_period": _c("coverage", "blocker", False, None, (), "Admission is outside the cover period"),
    "coverage.waiting_period": _c("coverage", "blocker", False, None, (), "Waiting period not completed"),
    "coverage.pre_existing": _c("coverage", "blocker", False, None, (), "Pre-existing condition within its waiting period"),
    "coverage.exclusion": _c("coverage", "blocker", False, None, (), "Diagnosis falls under a policy exclusion"),
    "coverage.sub_limit": _c("coverage", "warning", False, None, (), "A sub-limit applies"),
    "coverage.sum_insured_exhausted": _c("coverage", "blocker", False, None, (), "Sum insured exhausted"),
    "coverage.sum_insured_low": _c("coverage", "warning", False, None, (), "Remaining sum insured is below the claimed amount"),
    "coverage.not_network": _c("coverage", "warning", False, None, (), "Hospital is not in the network"),
    # calculation
    "calc.rule_conflict": _c("calculation", "blocker", False, None, (), "Calculation rules conflict"),
    "calc.unmapped_line": _c("calculation", "warning", False, None, (), "Bill lines could not be mapped to a rule group"),
    "calc.negative_payable": _c("calculation", "blocker", False, None, (), "Calculation produced a negative payable"),
    "calc.blocked": _c("calculation", "blocker", False, None, (), "Calculation blocked the whole claim"),
    # engine / agents
    "step.agent_unavailable": _c("*", "warning", False, None, (), "Automated analysis unavailable; manual check required"),
    "step.agent_invalid_output": _c("*", "blocker", False, None, (), "Automated analysis returned an invalid result; manual check required"),
    "agent.disagrees_with_rules": _c("*", "warning", False, None, (), "The agent's view differs from the deterministic check"),
    "step.skipped_due_to": _c("*", "info", False, None, (), "Step skipped because a prerequisite failed"),
}


def known(code: str) -> bool:
    return code in CODES or code.startswith("step.skipped_due_to.")


def info(code: str) -> CodeInfo:
    if code.startswith("step.skipped_due_to."):
        return CODES["step.skipped_due_to"]
    return CODES[code]
