"""Enums shared by hospital and insurer (01-02 section 4). Values are lower-snake strings."""

from enum import EnumType, StrEnum


class _LowerAttr(EnumType):
    """`ClaimType.cashless` resolves to `ClaimType.CASHLESS` (Dev B's insurer code uses lower-case member names).
    A lookup fallback rather than real aliases, so schema generation still sees each value once."""

    def __getattr__(cls, name: str):  # type: ignore[no-untyped-def]
        try:
            return cls._member_map_[name.upper()]
        except KeyError:
            raise AttributeError(name) from None


class ClaimType(StrEnum, metaclass=_LowerAttr):
    CASHLESS = "cashless"
    REIMBURSEMENT = "reimbursement"


class AdmissionType(StrEnum, metaclass=_LowerAttr):
    PLANNED = "planned"
    EMERGENCY = "emergency"


class Gender(StrEnum):
    M = "M"
    F = "F"
    O = "O"  # noqa: E741


class DocType(StrEnum, metaclass=_LowerAttr):
    PRESCRIPTION = "prescription"
    PROCEDURE_BILL = "procedure_bill"
    DISCHARGE_SUMMARY = "discharge_summary"
    FINAL_BILL = "final_bill"
    ITEMISED_BILL = "itemised_bill"
    PHARMACY_BILL = "pharmacy_bill"
    LAB_REPORT = "lab_report"
    RADIOLOGY_REPORT = "radiology_report"
    INVESTIGATION_REPORT = "investigation_report"
    ADMISSION_NOTE = "admission_note"
    PREAUTH_APPROVAL = "preauth_approval"
    CLAIM_FORM = "claim_form"
    ID_PROOF = "id_proof"
    POLICY_CARD = "policy_card"
    CANCELLED_CHEQUE = "cancelled_cheque"
    IMPLANT_STICKER = "implant_sticker"
    PAYMENT_RECEIPT = "payment_receipt"
    FIR_MLC = "fir_mlc"
    OTHER = "other"


class BillCategory(StrEnum, metaclass=_LowerAttr):
    ROOM = "room"
    ICU = "icu"
    SURGERY = "surgery"
    ANAESTHESIA = "anaesthesia"
    MEDICINE = "medicine"
    CONSUMABLE = "consumable"
    IMPLANT = "implant"
    INVESTIGATION = "investigation"
    CONSULTATION = "consultation"
    OTHER = "other"


class HospitalCaseStatus(StrEnum, metaclass=_LowerAttr):
    DRAFT = "draft"
    DOCS_PENDING = "docs_pending"
    DOCS_COMPLETE = "docs_complete"
    BUILDING_CLAIM = "building_claim"
    READY_FOR_REVIEW = "ready_for_review"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    UNDER_QUERY = "under_query"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    SETTLED = "settled"
    CLOSED = "closed"


class InsurerCaseStatus(StrEnum, metaclass=_LowerAttr):
    RECEIVED = "received"
    VERIFYING = "verifying"
    NEEDS_INFO = "needs_info"
    READY_FOR_DECISION = "ready_for_decision"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    SETTLED = "settled"
    CLOSED = "closed"


class QueryStatus(StrEnum, metaclass=_LowerAttr):
    OPEN = "open"
    DRAFT_READY = "draft_ready"
    ANSWERED = "answered"
    CLOSED = "closed"
    ESCALATED = "escalated"


class QueryCategory(StrEnum, metaclass=_LowerAttr):
    MISSING_DOCUMENT = "missing_document"
    ILLEGIBLE_DOCUMENT = "illegible_document"
    IDENTITY_MISMATCH = "identity_mismatch"
    MEDICAL_CLARIFICATION = "medical_clarification"
    BILLING_DISCREPANCY = "billing_discrepancy"
    POLICY_EXCLUSION = "policy_exclusion"
    OTHER = "other"


class DecisionOutcome(StrEnum, metaclass=_LowerAttr):
    APPROVE = "approve"
    PARTIAL = "partial"
    REJECT = "reject"
    NEEDS_INFO = "needs_info"


class Severity(StrEnum, metaclass=_LowerAttr):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ActorType(StrEnum, metaclass=_LowerAttr):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"
    EXTERNAL = "external"


class SettlementMode(StrEnum):
    NEFT = "NEFT"
    RTGS = "RTGS"
    IMPS = "IMPS"
    CHEQUE = "CHEQUE"


class WithdrawReason(StrEnum, metaclass=_LowerAttr):
    PATIENT_REQUESTED = "patient_requested"
    DUPLICATE = "duplicate"
    HOSPITAL_ERROR = "hospital_error"
    OTHER = "other"


class DocSupplementReason(StrEnum, metaclass=_LowerAttr):
    QUERY_RESPONSE = "query_response"
    VOLUNTARY = "voluntary"
    CORRECTION = "correction"


class StepName(StrEnum):
    """Insurer verification steps (03-03)."""

    document_fetch = "document_fetch"
    completeness = "completeness"
    identity = "identity"
    authenticity = "authenticity"
    coverage = "coverage"
    calculation = "calculation"


# Cross-side mapping (01-02 section 8.4). Hospital-visible status for each insurer status.
INSURER_TO_HOSPITAL: dict[InsurerCaseStatus, HospitalCaseStatus] = {
    InsurerCaseStatus.RECEIVED: HospitalCaseStatus.ACKNOWLEDGED,
    InsurerCaseStatus.VERIFYING: HospitalCaseStatus.ACKNOWLEDGED,
    InsurerCaseStatus.READY_FOR_DECISION: HospitalCaseStatus.ACKNOWLEDGED,
    InsurerCaseStatus.AWAITING_APPROVAL: HospitalCaseStatus.ACKNOWLEDGED,
    InsurerCaseStatus.NEEDS_INFO: HospitalCaseStatus.UNDER_QUERY,
    InsurerCaseStatus.ESCALATED: HospitalCaseStatus.UNDER_QUERY,
    InsurerCaseStatus.APPROVED: HospitalCaseStatus.APPROVED,
    InsurerCaseStatus.PARTIALLY_APPROVED: HospitalCaseStatus.PARTIALLY_APPROVED,
    InsurerCaseStatus.REJECTED: HospitalCaseStatus.REJECTED,
    InsurerCaseStatus.SETTLED: HospitalCaseStatus.SETTLED,
    InsurerCaseStatus.CLOSED: HospitalCaseStatus.CLOSED,
}
