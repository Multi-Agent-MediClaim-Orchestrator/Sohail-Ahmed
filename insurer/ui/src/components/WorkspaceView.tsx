"use client";
import { useEffect, useState } from "react";
import { api, can, formatMoney } from "@/lib/api";
import type { Finding, Workspace } from "@/lib/types";
import { DecisionForm } from "./DecisionForm";
import { DocumentViewer } from "./DocumentViewer";
import { QueriesPanel } from "./QueriesPanel";
import type { StreamState } from "@/lib/useEventStream";

const STEP_LABEL: Record<string, string> = { document_fetch: "Fetch", completeness: "Completeness", identity: "Identity", authenticity: "Authenticity", coverage: "Coverage", calculation: "Calculation" };

export function Chip({ kind, children }: { kind?: string; children: React.ReactNode }) {
  return <span className={`chip chip-${kind ?? "neutral"}`}>{children}</span>;
}

function FindingRow({ f, caseId, roles, overridden, onChanged }: { f: Finding; caseId: string; roles: string[]; overridden: boolean; onChanged?: () => void }) {
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const canOverride = can(roles, "reviewer", "senior_reviewer") && f.severity !== "info" && !overridden;
  return (
    <li>
      <Chip kind={f.severity}>{f.severity}</Chip> <code>{f.code}</code> {f.message} {f.fixable && <Chip kind="info">fixable</Chip>} {overridden && <Chip>overridden</Chip>}
      {canOverride && !asking && <button onClick={() => setAsking(true)}>Override</button>}
      {asking && (
        <span>
          <input aria-label={`Override reason for ${f.code}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (min 10 characters)" />
          <button disabled={reason.trim().length < 10} onClick={async () => { await api(`/v1/cases/${caseId}/findings/${encodeURIComponent(f.key ?? f.code)}/override`, { method: "POST", body: { reason } }); setAsking(false); onChanged?.(); }}>Confirm override</button>
        </span>
      )}
    </li>
  );
}

export function WorkspaceView({ ws, roles, onChanged, stream }: { ws: Workspace; roles: string[]; onChanged?: () => void; stream?: StreamState }) {
  const [open, setOpen] = useState<string | null>(ws.run?.steps.find((s) => s.status !== "passed")?.step ?? null);
  const [selDoc, setSelDoc] = useState<string | null>(ws.documents[0]?.id ?? null);
  const [help, setHelp] = useState(false);
  const steps = ws.run?.steps ?? [];

  // keyboard shortcuts (disabled while typing): j/k document, f toggle findings, n next open finding, r re-run, ? help
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      const i = ws.documents.findIndex((d) => d.id === selDoc);
      if (e.key === "j" && ws.documents.length) setSelDoc(ws.documents[Math.min(ws.documents.length - 1, i + 1)].id);
      else if (e.key === "k" && ws.documents.length) setSelDoc(ws.documents[Math.max(0, i - 1)].id);
      else if (e.key === "f") setOpen((o) => (o ? null : steps.find((s) => s.findings.length)?.step ?? steps[0]?.step ?? null));
      else if (e.key === "n") {
        const withOpen = steps.filter((s) => s.findings.some((f) => f.severity !== "info"));
        if (withOpen.length) setOpen(withOpen[(withOpen.findIndex((s) => s.step === open) + 1) % withOpen.length].step);
      } else if (e.key === "r" && can(roles, "reviewer", "senior_reviewer")) void api(`/v1/cases/${ws.case.id}/verification/rerun`, { method: "POST", body: {} }).then(() => onChanged?.());
      else if (e.key === "?") setHelp((h) => !h);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ws.documents, ws.case.id, selDoc, steps, open, roles, onChanged]);
  const allFindings = steps.flatMap((s) => s.findings);
  const overriddenCodes = new Set(ws.overrides.map((o) => o.finding_code));
  const c = ws.case;
  return (
    <div className="workspace">
      <header className="ws-header">
        <h1>{c.insurer_claim_no}</h1>
        <Chip>{c.status}</Chip> <Chip>{c.claim_type}</Chip> <Chip>{c.admission_type}</Chip>
        <span>Member {ws.submission.patient.full_name_masked} · {ws.submission.patient.member_id_masked}</span>
        <span>Claimed {formatMoney(c.claimed_amount)}</span>
        {ws.recommendation && <span>Recommended {formatMoney(ws.recommendation.approved_amount)}</span>}
        {c.degraded && <Chip kind="warning">degraded</Chip>}
        {stream && <Chip kind={stream === "live" ? "ok" : "warning"}>{stream === "live" ? "live" : stream === "paused" ? "paused" : "connecting"}</Chip>}
        {c.sla_due_at && <span title={c.sla_due_at}>SLA {new Date(c.sla_due_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST</span>}
      </header>
      <div className="panes">
        <section aria-label="Documents" className="pane">
          <h2>Documents</h2>
          <DocumentViewer caseId={ws.case.id} docs={ws.documents} findings={allFindings} selected={selDoc} onSelect={setSelDoc} onFindingClick={(code) => setOpen(steps.find((st) => st.findings.some((f) => f.code === code))?.step ?? null)} />
        </section>
        <section aria-label="Verification" className="pane">
          <h2>Verification</h2>
          {ws.run?.steps.map((s) => (
            <article key={s.step}>
              <button aria-expanded={open === s.step} onClick={() => setOpen(open === s.step ? null : s.step)}>
                {STEP_LABEL[s.step] ?? s.step} <Chip kind={s.status}>{s.status}</Chip>
              </button>
              {open === s.step && (
                <div>
                  {s.deterministic && <pre aria-label="Deterministic checks">{JSON.stringify(s.deterministic, null, 1)}</pre>}
                  <ul>{s.findings.map((f, i) => <FindingRow key={i} f={f} caseId={ws.case.id} roles={roles} overridden={overriddenCodes.has(f.code)} onChanged={onChanged} />)}</ul>
                  {s.degraded && <p className="banner">Automated explanation unavailable; deterministic checks shown. Manual check required.</p>}
                  {s.agent_note && <details><summary>AI explanation — not used for gating</summary><p>{s.agent_note}</p></details>}
                </div>
              )}
            </article>
          )) ?? <p className="muted">No verification run yet.</p>}
        </section>
        <section aria-label="Decision" className="pane">
          <h2>Decision</h2>
          <table aria-label="Calculation breakdown">
            <thead><tr><th>Line</th><th>Category</th><th>Claimed</th><th>Disallowed</th><th>Payable</th><th>Rules</th></tr></thead>
            <tbody>
              {ws.bill_lines.map((l) => (
                <tr key={l.line_ref}>
                  <td>{l.description}</td><td>{l.category}</td><td>{formatMoney(l.amount)}</td>
                  <td>{formatMoney(l.deduction?.disallowed)}</td><td>{formatMoney(l.deduction?.payable)}</td>
                  <td>{l.deduction?.rule_ids.map((r) => <Chip key={r}>{r}</Chip>)}</td>
                </tr>
              ))}
            </tbody>
            {ws.calc && <tfoot><tr><td colSpan={4}>Total payable (engine {ws.calc.engine_version}, rules v{ws.calc.policy_rules_version})</td><td colSpan={2}>{formatMoney(ws.calc.payable_total)}</td></tr></tfoot>}
          </table>
          {ws.recommendation?.explanation && <p>{ws.recommendation.explanation}</p>}
          {ws.final_decision ? <p role="status">Final: {ws.final_decision.outcome} {formatMoney(ws.final_decision.approved_amount)}</p> : <DecisionForm ws={ws} roles={roles} onDone={onChanged} />}
          <QueriesPanel caseId={ws.case.id} roles={roles} />
        </section>
      </div>
      {help && <div role="dialog" aria-label="Keyboard shortcuts"><p>j/k document · f toggle findings · n next open finding · r re-run verification · ? this help</p></div>}
      <footer className="ws-footer">
        {ws.audit_tail.map((a) => <span key={a.seq}>#{a.seq} {a.event_type} </span>)}
      </footer>
    </div>
  );
}
