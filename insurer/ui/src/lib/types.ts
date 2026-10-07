import type { Money } from "./api";

export type Severity = "info" | "warning" | "blocker";
export type Evidence = { doc_id: string; page?: number; bbox?: [number, number, number, number] };
export type Finding = { code: string; severity: Severity; message?: string; fixable?: boolean; key?: string; evidence?: Evidence[] };
export type Step = { step: string; status: string; score: number | null; findings: Finding[]; deterministic: Record<string, unknown> | null; agent_note: string | null; degraded: boolean };
export type DocItem = { id: string; doc_type: string; filename: string; pages: number | null; fetch_status: string; scan_result: string | null; parse_confidence: number | null; view_url: string | null; superseded: boolean };
export type BillLine = { line_no: number; line_ref: string; description: string; category: string; amount: Money; deduction: { disallowed: Money; payable: Money; rule_ids: string[] } | null };
export type Recommendation = { outcome: string; approved_amount: Money; reason_codes: string[]; explanation: string | null; gate_tier: string; flags: string[] };
export type QuerySummary = { id: string; round: number; status: string; category: string; due_by: string };

export type Workspace = {
  case: { id: string; insurer_claim_no: string; hospital_claim_ref: string; status: string; priority: number; sla_due_at: string | null; etag: string; assigned_reviewer: string | null; claim_type: string; admission_type: string; claimed_amount: Money; degraded: boolean };
  submission: { patient: { full_name_masked: string; dob_year: string; gender: string; member_id_masked: string }; admission: Record<string, unknown>; totals: Record<string, unknown> };
  bill_lines: BillLine[];
  documents: DocItem[];
  run: { id: string; run_no: number; status: string; outcome: string | null; steps: Step[] } | null;
  calc: { engine_version: string; policy_rules_version: number; payable_total: Money; flags: { code: string; message?: string }[]; blocked: unknown; trace: unknown } | null;
  recommendation: Recommendation | null;
  queries: QuerySummary[];
  decisions: { id: string; kind: string; status: string; outcome: string; approved_amount: Money; tier: string }[];
  final_decision: { outcome: string; approved_amount: Money } | null;
  overrides: { finding_code: string; by: string; reason: string }[];
  audit_tail: { seq: number; event_type: string; ts: string; actor_id: string }[];
};

export type CaseRow = { id: string; insurer_claim_no: string; hospital_name: string; claim_type: string; status: string; claimed_amount: Money; priority: number; sla_due_at: string | null; assigned_reviewer: string | null };
export type ApprovalItem = { decision_id: string; case_id: string; insurer_claim_no: string; amount: Money; tier: string; approvals: number; needed: number };
export type EscalationItem = { id: string; case_id: string; insurer_claim_no: string; reason: string; opened_at: string; unresolved: number };
export type SettlementRow = { id: string; case_id: string; amount: Money; status: string; utr: string | null };

export type QueryFull = {
  id: string; round: number; category: string; text: string; requested_doc_types: string[]; status: string; origin: string; draft_source: string | null;
  citations: { type?: string; ref?: string; snippet?: string }[] | null; lint_errors: { code: string; message?: string }[] | null; due_by: string; sent_at: string | null;
  finding_keys: string[]; response: { answer_text: string; attached_doc_ids: string[]; triage: { verdict?: string; notes?: string } | null } | null;
};
export type ConfigVersion = { version: number; status: string; change_note?: string; effective_from: string | null; created_by?: string };
