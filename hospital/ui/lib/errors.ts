export interface ApiProblem { status: number; code: string; title?: string; detail?: string; errors?: { field?: string; message?: string; code?: string }[]; trace_id?: string }
export class ApiError extends Error {
  constructor(public problem: ApiProblem) { super(problem.detail || problem.title || problem.code); }
}
const FRIENDLY: Record<string, string> = {
  version_conflict: "Someone changed this just now. Reload to see the latest.",
  precondition_failed: "Someone changed this just now. Reload to see the latest.",
  invalid_transition: "That action is not allowed in the current state.",
  forbidden: "Your role does not allow this.",
  not_found: "We could not find that.",
  rate_limited: "Too many requests. Wait a moment and try again.",
  case_locked: "This case is locked and cannot take new documents.",
  docs_incomplete: "The checklist still has outstanding items.",
  signoff_required: "An officer sign-off is required first.",
  signoff_stale: "The claim changed after sign-off. Sign off again.",
  warnings_not_acknowledged: "Acknowledge the routing warnings first.",
  four_eyes_required: "A different officer must sign off.",
  override_note_required: "Add a note (at least 20 characters) to approve a flagged draft.",
  not_approved: "The response needs its approvals first.",
  duplicate_approver: "You already approved this; a second officer is needed.",
  crew_unavailable: "The drafting assistant is unavailable. Write the reply by hand.",
  unauthenticated: "Please sign in again.",
};
export function message(e: unknown): string {
  if (e instanceof ApiError) return FRIENDLY[e.problem.code] ?? e.problem.detail ?? e.problem.title ?? e.problem.code;
  return e instanceof Error ? e.message : "Something went wrong.";
}
