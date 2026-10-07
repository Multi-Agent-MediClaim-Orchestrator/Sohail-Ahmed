"use client";
import { useState } from "react";
import { api, ApiError, can, formatMoney } from "@/lib/api";
import type { Workspace } from "@/lib/types";

const TIER_TEXT: Record<string, string> = {
  auto: "Eligible for automatic approval (all checks clean, amount within the auto limit).",
  reviewer: "A reviewer can finalise this decision.",
  single_approver: "Requires one approver.",
  dual_approver: "Requires two approvers including one senior.",
};

/** Gate text is rendered from the server's tier; nothing is computed here. */
export function GateBanner({ tier }: { tier: string | undefined }) {
  if (!tier) return null;
  return (
    <div role="status" className="banner" data-tier={tier}>
      {TIER_TEXT[tier] ?? `Gate tier: ${tier}`}
    </div>
  );
}

export function DecisionForm({ ws, roles, onDone }: { ws: Workspace; roles: string[]; onDone?: () => void }) {
  const rec = ws.recommendation;
  const [outcome, setOutcome] = useState(rec?.outcome ?? "approve");
  const [amount, setAmount] = useState(rec?.approved_amount ?? "0.00");
  const [note, setNote] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  if (!can(roles, "reviewer", "senior_reviewer")) return <p className="muted">You do not have permission to submit decisions.</p>;
  if (ws.case.status !== "ready_for_decision") return <p className="muted">Decision available when the case is ready for decision.</p>;

  const changed = !!rec && (outcome !== rec.outcome || amount !== rec.approved_amount); // wording only: the server decides what is allowed
  const needsReason = changed && overrideReason.trim().length < 10;

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      const res = await api<{ status?: string }>(`/v1/cases/${ws.case.id}/decision/submit`, {
        method: "POST", etag: ws.case.etag,
        body: { outcome, approved_amount: amount, reason_codes: rec?.reason_codes ?? [], note: note || null, override_reason: changed ? overrideReason : null },
      });
      setOk(res.status ?? "submitted");
      onDone?.();
    } catch (e) {
      setErr(e instanceof ApiError ? (e.status === 412 ? "This case changed while you were viewing it. Reload and review again." : `${e.code}: ${e.message}`) : "Unexpected error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => { e.preventDefault(); void submit(); }} aria-label="Decision form">
      <GateBanner tier={rec?.gate_tier} />
      <p>System recommendation: <strong>{rec?.outcome ?? "—"}</strong> {formatMoney(rec?.approved_amount)}</p>
      <label>Outcome
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          {["approve", "partial", "reject", "needs_info"].map((o) => <option key={o}>{o}</option>)}
        </select>
      </label>
      <label>Approved amount
        <input inputMode="decimal" pattern="^\d+(\.\d{1,2})?$" value={amount} onChange={(e) => setAmount(e.target.value)} disabled={outcome === "reject"} />
      </label>
      {changed && (
        <label>Reason for changing the recommendation (min 10 characters)
          <textarea value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} />
        </label>
      )}
      <label>Note (internal)
        <textarea value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {err && <p role="alert" className="error">{err}</p>}
      {ok && <p role="status">Submitted: {ok}</p>}
      <button type="submit" disabled={busy || needsReason}>Confirm decision</button>
    </form>
  );
}
