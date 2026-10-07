import type { ApprovalItem, CaseRow, ConfigVersion, EscalationItem, QueryFull, SettlementRow, Workspace } from "@/lib/types";

export const CASE_ID = "0191f0a2-0000-7000-8000-000000000001";

export const cases: CaseRow[] = [
  { id: CASE_ID, insurer_claim_no: "IC-2026-000101", hospital_name: "Sunrise Hospital", claim_type: "cashless", status: "ready_for_decision", claimed_amount: "620000.00", priority: 2, sla_due_at: "2026-10-09T10:00:00Z", assigned_reviewer: "rev-1" },
  { id: "0191f0a2-0000-7000-8000-000000000002", insurer_claim_no: "IC-2026-000102", hospital_name: "Lotus Care", claim_type: "reimbursement", status: "needs_info", claimed_amount: "42000.00", priority: 3, sla_due_at: null, assigned_reviewer: null },
];

export const workspace: Workspace = {
  case: { id: CASE_ID, insurer_claim_no: "IC-2026-000101", hospital_claim_ref: "HC-2026-000123", status: "ready_for_decision", priority: 2, sla_due_at: "2026-10-09T10:00:00Z", etag: "etag-1", assigned_reviewer: "rev-1", claim_type: "cashless", admission_type: "planned", claimed_amount: "620000.00", degraded: false },
  submission: { patient: { full_name_masked: "R*** K****", dob_year: "1980", gender: "M", member_id_masked: "****4821" }, admission: {}, totals: {} },
  bill_lines: [
    { line_no: 1, line_ref: "L0001", description: "Private room 5 days", category: "room", amount: "50000.00", deduction: { disallowed: "10000.00", payable: "40000.00", rule_ids: ["R-ROOM-CAP"] } },
    { line_no: 2, line_ref: "L0002", description: "Coronary stent", category: "implant", amount: "150000.00", deduction: { disallowed: "0.00", payable: "150000.00", rule_ids: [] } },
    { line_no: 3, line_ref: "L0003", description: "Registration fee", category: "other", amount: "500.00", deduction: { disallowed: "500.00", payable: "0.00", rule_ids: ["R-NONMED"] } },
  ],
  documents: [
    { id: "d1", doc_type: "discharge_summary", filename: "discharge.pdf", pages: 3, fetch_status: "fetched", scan_result: "clean", parse_confidence: 0.94, view_url: null, superseded: false },
    { id: "d2", doc_type: "final_bill", filename: "bill.pdf", pages: 2, fetch_status: "fetched", scan_result: "clean", parse_confidence: 0.88, view_url: null, superseded: false },
  ],
  run: {
    id: "run-1", run_no: 1, status: "completed", outcome: "ready_for_decision",
    steps: [
      { step: "completeness", status: "passed", score: null, findings: [], deterministic: { required: 6, present: 6 }, agent_note: null, degraded: false },
      { step: "identity", status: "passed", score: 0.93, findings: [], deterministic: { name_similarity: 0.93, threshold: 0.85 }, agent_note: "Initial expanded: R. Kumar -> Ravi Kumar.", degraded: false },
      { step: "authenticity", status: "warning", score: null, findings: [{ code: "STAMP_MISSING", severity: "warning", message: "Hospital stamp not found on final bill", fixable: true, key: "auth.stamp_missing", evidence: [{ doc_id: "d2", page: 1, bbox: [0.6, 0.8, 0.9, 0.95] }] }], deterministic: null, agent_note: null, degraded: true },
      { step: "calculation", status: "passed", score: null, findings: [], deterministic: null, agent_note: null, degraded: false },
    ],
  },
  calc: { engine_version: "1.0.0", policy_rules_version: 12, payable_total: "190000.00", flags: [], blocked: null, trace: [] },
  recommendation: { outcome: "partial", approved_amount: "190000.00", reason_codes: ["ROOM_RENT_CAP", "NON_MEDICAL"], explanation: "Room rent capped; non-medical items removed.", gate_tier: "single_approver", flags: [] },
  queries: [{ id: "q1", round: 1, status: "answered", category: "missing_document", due_by: "2026-10-08T10:00:00Z" }],
  decisions: [], final_decision: null, overrides: [],
  audit_tail: [{ seq: 12, event_type: "decision.recommended", ts: "2026-10-07T09:00:00Z", actor_id: "system" }],
};

export const approvals: ApprovalItem[] = [{ decision_id: "dec-1", case_id: CASE_ID, insurer_claim_no: "IC-2026-000101", amount: "190000.00", tier: "single_approver", approvals: 0, needed: 1 }];
export const escalations: EscalationItem[] = [{ id: "esc-1", case_id: "0191f0a2-0000-7000-8000-000000000002", insurer_claim_no: "IC-2026-000102", reason: "round3_unresolved", opened_at: "2026-10-06T08:00:00Z", unresolved: 2 }];
export const settlements: SettlementRow[] = [{ id: "st-1", case_id: CASE_ID, amount: "190000.00", status: "failed", utr: null }];

export const queriesFull: QueryFull[] = [
  { id: "q1", round: 1, category: "missing_document", text: "Please upload the stamped final bill for this admission so we can continue processing.", requested_doc_types: ["final_bill"], status: "draft_ready", origin: "agent_draft", draft_source: "llm",
    citations: [{ type: "finding", ref: "auth.stamp_missing", snippet: "Hospital stamp not found" }], lint_errors: [], due_by: "2026-10-12T10:00:00Z", sent_at: null, finding_keys: ["auth.stamp_missing"], response: null },
  { id: "q0", round: 1, category: "missing_document", text: "Earlier request.", requested_doc_types: [], status: "answered", origin: "human", draft_source: null, citations: [], lint_errors: [], due_by: "2026-10-08T10:00:00Z", sent_at: "2026-10-07T10:00:00Z",
    finding_keys: [], response: { answer_text: "Attached.", attached_doc_ids: [], triage: { verdict: "sufficient", notes: "all requested items present" } } },
];
export const configVersions: ConfigVersion[] = [
  { version: 3, status: "draft", change_note: "raise T_auto", effective_from: null },
  { version: 2, status: "published", change_note: "initial", effective_from: "2026-01-01T00:00:00Z" },
];
