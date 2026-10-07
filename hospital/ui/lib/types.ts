// Shapes the hospital API returns (subset the UI reads). The API is authoritative; the UI only displays.
export interface Me { id: string; email: string; name: string; roles: string[]; capabilities: string[] }
export interface CaseRow { id: string; claim_ref: string; patient_name: string; claim_type: string; status: string; flags: string[]; assigned_to: string | null; claimed_amount: string | null; doc_counts: Record<string, number>; updated_at: string }
export interface CaseList { items: CaseRow[]; next_cursor: string | null; total_estimate: number }
export interface RouteWarning { code: string; message: string; needs_ack: boolean }
export interface CaseView {
  id: string; claim_ref: string; status: string; version: number; claim_type: string; admission_type: string;
  patient: { id: string; uhid: string; full_name: string; dob: string; gender: string; phone_last4: string };
  policy: { insurer_name: string; policy_number: string; member_id: string };
  route: { decision?: { pipeline: string; flags: string[]; intimation_deadline: string | null; preauth_by: string | null }; warnings?: RouteWarning[] } | null;
  flags: string[]; admitted_on: string | null; discharged_on: string | null; preauth_ref: string | null;
  filing_deadline: string | null; insurer_claim_no: string | null; claimed_amount: string | null; allowed_transitions: string[];
  created_at: string; updated_at: string;
}
export interface DocView {
  id: string; filename: string; mime_type: string; size_bytes: number; pages: number | null; scan_status: string; lifecycle: string;
  doc_type: string | null; parse_status: string; parse_confidence: number | null; classification_confidence: number | null;
  quality: { score: number | null; flags: string[]; has_required_stamp: boolean | null }; usable: boolean; needs_attention: boolean; created_at: string;
}
export interface CheckItem { requirement: string; rule_id: string; doc_type: string | null; status: string; severity: string; message: string; reasons: string[]; document_ids: string[] }
export interface DocRequest { id: string; doc_type: string; message: string; due_by: string; reminders_sent: number }
export interface Completeness { complete: boolean; provisional: boolean; summary: Record<string, number>; items: CheckItem[]; open_doc_requests: DocRequest[] }
export interface Finding { code: string; field?: string; message: string; severity?: string }
export interface BillLine { line_no: number; code?: string; description: string; category: string; qty: string; unit_price: string; amount: string; service_date?: string; source_doc_id: string; source_page?: number }
export interface ClaimPayload {
  patient: { full_name: string; dob: string; gender: string; member_id: string; policy_number: string };
  admission: { admission_type: string; admitted_on: string; discharged_on: string; diagnosis_codes: string[]; procedure_codes: string[]; treating_doctor: string };
  bill_lines: BillLine[]; totals: { gross: string; discounts: string; claimed: string }; documents: string[];
}
export interface ClaimView {
  version: number; source: string; has_errors: boolean; payload: ClaimPayload; etag: string;
  validation: { errors: Finding[]; warnings: Finding[] }; signoff: { decision: string; officer?: string } | null;
  ready_to_submit?: { ok: boolean; reasons: string[] };
}
export interface Submission { status: string | null; attempts: number; last_error: string | null; next_attempt_at: string | null; insurer_claim_no: string | null; acknowledged_at: string | null; ready_to_submit: { ok: boolean; reasons: string[] } }
export interface TimelineEvent { ts: string; kind: string; from: string | null; to: string | null; actor: string }  // `to` is null for events that are not status changes
export interface QueryRow { id: string; case_id: string; claim_ref: string; round: number; category: string; status: string; due_by: string | null; escalation_risk: boolean; patient_initials: string; overdue: boolean }
export interface QueryResp { version: number; status: string; source: string; draft_text: string; attached_doc_ids: string[]; citations: { source_id: string; quote: string }[] | null; unsupported_claims: { rule: string; detail: string }[] | null; approved_by: string | null; second_approver: string | null; override_note: string | null }
export interface QueryDetail { id: string; case_id: string; claim_ref: string; round: number; category: string; text: string; status: string; due_by: string | null; requested_doc_types: string[]; triage: { action?: string; needs_docs?: boolean; note?: string } | null; triage_source: string | null; escalation_risk: boolean; approvals_needed: number; responses: QueryResp[] }
export interface Dashboard { cases_by_status: Record<string, number>; queries_by_status: Record<string, number>; overdue_queries: number; overdue_requests: number; deadlines: { case_id: string; claim_ref: string; filing_deadline: string }[] }
