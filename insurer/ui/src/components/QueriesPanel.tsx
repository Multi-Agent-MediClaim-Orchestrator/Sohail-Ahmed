"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, can } from "@/lib/api";
import type { QueryFull } from "@/lib/types";

const MAX = 1200;

function QueryCard({ q, roles, onChanged }: { q: QueryFull; roles: string[]; onChanged: () => void }) {
  const [text, setText] = useState(q.text);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const editable = q.status === "draft_ready" && can(roles, "reviewer", "senior_reviewer");
  const blockers = (q.lint_errors ?? []).length > 0;
  const fail = (e: unknown) => setErr(e instanceof ApiError ? `${e.code}: ${e.message}` : "Failed");

  async function save() {
    try { await api(`/v1/queries/${q.id}`, { method: "PATCH", body: { text } }); setErr(null); onChanged(); } catch (e) { fail(e); }
  }
  async function send() {
    try { await api(`/v1/queries/${q.id}/send`, { method: "POST", body: {} }); setConfirm(false); onChanged(); } catch (e) { fail(e); }
  }

  return (
    <article aria-label={`Query round ${q.round}`}>
      <h4>Round {q.round} of 3 — {q.category} <span className="chip">{q.status}</span> {q.draft_source === "llm" && <span className="chip chip-info">AI draft</span>}</h4>
      {editable ? (
        <>
          <textarea aria-label="Query text" value={text} maxLength={MAX} onChange={(e) => setText(e.target.value)} rows={6} />
          <p className="muted">{text.length}/{MAX}</p>
        </>
      ) : <p>{q.text}</p>}
      {q.requested_doc_types.length > 0 && <p>Documents requested: {q.requested_doc_types.join(", ")}</p>}
      {blockers && <ul role="alert">{q.lint_errors!.map((l, i) => <li key={i} className="error">{l.code} {l.message}</li>)}</ul>}
      {(q.citations ?? []).length > 0 && <details><summary>Grounding ({q.citations!.length})</summary><ul>{q.citations!.map((c, i) => <li key={i}>{c.ref}: {c.snippet}</li>)}</ul></details>}
      {q.response && (
        <section aria-label="Hospital response">
          <p><strong>Hospital reply:</strong> {q.response.answer_text}</p>
          {q.response.triage && <p>Triage: {q.response.triage.verdict} — {q.response.triage.notes}</p>}
        </section>
      )}
      {err && <p role="alert" className="error">{err}</p>}
      {editable && (
        <>
          <button onClick={save} disabled={text === q.text}>Save edits</button>{" "}
          <button onClick={() => setConfirm(true)} disabled={blockers}>Send to hospital…</button>
        </>
      )}
      {confirm && (
        <div role="dialog" aria-label="Confirm send">
          <p>The hospital will see exactly this text and can respond until {new Date(q.due_by).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST.</p>
          <button onClick={send}>Confirm send</button> <button onClick={() => setConfirm(false)}>Cancel</button>
        </div>
      )}
    </article>
  );
}

export function QueriesPanel({ caseId, roles }: { caseId: string; roles: string[] }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["queries", caseId], queryFn: () => api<{ items: QueryFull[] }>(`/v1/cases/${caseId}/queries`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["queries", caseId] });
  return (
    <section aria-label="Queries">
      <h3>Queries</h3>
      {q.data?.items.length === 0 && <p className="muted">No queries yet.</p>}
      {q.data?.items.map((x) => <QueryCard key={x.id} q={x} roles={roles} onChanged={() => void refresh()} />)}
      {can(roles, "reviewer", "senior_reviewer") && (
        <button onClick={async () => { await api(`/v1/cases/${caseId}/queries/draft`, { method: "POST", body: {} }); void refresh(); }}>Draft query from open findings</button>
      )}
    </section>
  );
}
