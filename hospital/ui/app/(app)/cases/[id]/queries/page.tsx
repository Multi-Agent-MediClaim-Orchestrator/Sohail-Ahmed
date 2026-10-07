"use client";
import { Suspense, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { DocView, QueryDetail, QueryRow } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useAct, useRole, useToast } from "@/components/hooks";
import { useCountdown } from "@/lib/useCountdown";

const RULE_TEXT: Record<string, string> = { G01: "too short", G02: "makes a commitment", G03: "number not in the records", G04: "citation without a quote", G05: "quote not found in the records", G06: "numbers without a source", G07: "too long", G08: "contains a link" };

function Thread({ qid, caseId }: { qid: string; caseId: string }) {
  const { is, me } = useRole();
  const [toast, tone, notify] = useToast();
  const q = useQuery({ queryKey: ["queries", qid], queryFn: () => get<QueryDetail>(`/v1/queries/${qid}`) });
  const docs = useQuery({ queryKey: ["case", caseId, "documents"], queryFn: () => get<{ documents: DocView[] }>(`/v1/cases/${caseId}/documents`) });
  const act = useAct([["queries"], ["case", caseId]], notify);
  const [text, setText] = useState("");
  const [attached, setAttached] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [dirty, setDirty] = useState(false);
  const cd = useCountdown(q.data?.due_by ?? null);
  const latest = q.data?.responses.filter((r) => r.status !== "superseded").at(-1);
  useEffect(() => { if (latest && !dirty) { setText(latest.draft_text); setAttached(latest.attached_doc_ids); } }, [latest?.version, latest?.draft_text]); // eslint-disable-line react-hooks/exhaustive-deps
  if (q.isLoading) return <Skeleton />;
  if (q.error || !q.data) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const d = q.data;
  const closed = ["answered", "closed"].includes(d.status);
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const flagged = latest?.status === "needs_attention";
  const iApproved = latest?.approved_by && latest.approved_by === me?.id;
  const suggested = (d.requested_doc_types ?? []).flatMap((t) => docs.data?.documents.filter((x) => x.doc_type === t && x.usable) ?? []);
  return (
    <Card title={<span>Round {d.round} of 3 · {d.category.replace(/_/g, " ")}</span>} actions={<span className={`text-sm ${cd.overdue ? "font-semibold text-red-800" : ""}`}>{closed ? <Badge tone="good" icon="✓">{d.status}</Badge> : cd.text}</span>}>
      {d.round >= 3 && !closed && <p className="mb-2 rounded bg-amber-50 p-2 text-sm text-amber-900">! Final round: two different officers must approve.</p>}
      <blockquote className="rounded bg-slate-50 p-3 text-sm whitespace-pre-wrap">{d.text}</blockquote>
      <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-700">
        {d.triage?.action && <Badge tone="info">{d.triage.action.replace(/_/g, " ")}</Badge>}{d.triage_source && <span>triage by {d.triage_source}</span>}
        {d.requested_doc_types.length > 0 && <span>Asked for: {d.requested_doc_types.map((t) => t.replace(/_/g, " ")).join(", ")}</span>}
      </p>
      {!closed && (is("desk") || is("officer")) && (
        <div className="mt-4 space-y-3">
          <div className="flex items-center justify-between"><label htmlFor={`r-${qid}`} className="text-sm font-medium">Reply</label>
            <Button variant="secondary" disabled={act.isPending} onClick={() => act.mutate({ method: "POST", path: `/v1/queries/${qid}/draft`, body: {} }, { onSuccess: () => { setDirty(false); notify("Drafting… the reply appears here"); } })}>{latest ? "Regenerate draft" : "Draft a reply"}</Button></div>
          <textarea id={`r-${qid}`} className={inputCls} rows={7} value={text} onChange={(e) => { setText(e.target.value); setDirty(true); }} />
          <p className={`text-xs ${words > 250 ? "text-red-800" : "text-slate-600"}`}>{words} words (aim for 250 or fewer)</p>
          {latest?.unsupported_claims && latest.unsupported_claims.length > 0 && <ul role="alert" className="rounded bg-amber-50 p-2 text-sm text-amber-900">{latest.unsupported_claims.map((u, i) => <li key={i}>! {RULE_TEXT[u.rule] ?? u.rule}: {u.detail}</li>)}</ul>}
          {latest?.citations && latest.citations.length > 0 && <details className="text-xs"><summary className="cursor-pointer">Sources used ({latest.citations.length})</summary><ul className="mt-1 list-disc pl-5">{latest.citations.map((c, i) => <li key={i}>{c.source_id}: “{c.quote}”</li>)}</ul></details>}
          <fieldset><legend className="text-sm font-medium">Attach documents</legend>
            <div className="mt-1 flex flex-wrap gap-3 text-sm">{(docs.data?.documents ?? []).filter((x) => x.usable).map((x) => (
              <label key={x.id} className="flex items-center gap-1"><input type="checkbox" checked={attached.includes(x.id)} onChange={(e) => { setAttached(e.target.checked ? [...attached, x.id] : attached.filter((a) => a !== x.id)); setDirty(true); }} />{x.filename}{suggested.some((s) => s.id === x.id) && <Badge tone="info">asked for</Badge>}</label>))}</div></fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" disabled={!text.trim() || act.isPending} onClick={() => act.mutate({ method: "PUT", path: `/v1/queries/${qid}/response`, body: { draft_text: text, attached_doc_ids: attached, expected_version: latest?.version ?? null } }, { onSuccess: () => { setDirty(false); notify("Draft saved"); } })}>Save draft</Button>
            {is("officer") && latest && ["draft", "needs_attention"].includes(latest.status) && !dirty && (
              <>{flagged && !latest.override_note && <input aria-label="Override note" className={inputCls + " w-72"} placeholder="Why is this safe to send? (20+ characters)" value={note} onChange={(e) => setNote(e.target.value)} />}
                <Button disabled={act.isPending || !!iApproved || (flagged && !latest.override_note && note.trim().length < 20)} onClick={() => act.mutate({ method: "POST", path: `/v1/queries/${qid}/approve`, body: { override_note: note || null } })}>Approve ({latest.approved_by ? "2nd" : "1st"} of {d.approvals_needed})</Button></>)}
            {is("officer") && latest?.status === "approved" && <Button disabled={act.isPending} onClick={() => act.mutate({ method: "POST", path: `/v1/queries/${qid}/send` }, { onSuccess: () => notify("Sent to the insurer") })}>Send to insurer</Button>}
            {iApproved && latest?.status !== "approved" && <span className="text-xs text-slate-700">You approved. Another officer must approve before it can be sent.</span>}
          </div>
        </div>)}
      {closed && latest && <div className="mt-3"><p className="text-xs text-slate-600">Reply sent</p><p className="whitespace-pre-wrap rounded bg-slate-50 p-3 text-sm">{latest.draft_text}</p></div>}
      <Toast text={toast} tone={tone} />
    </Card>
  );
}

function Tab() {
  const { id } = useParams<{ id: string }>();
  const focus = useSearchParams().get("focus");
  const q = useQuery({ queryKey: ["queries", "case", id], queryFn: async () => (await get<{ items: QueryRow[] }>(`/v1/queries?limit=100`)).items.filter((x) => x.case_id === id) });
  if (q.isLoading) return <Skeleton />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const rows = [...(q.data ?? [])].sort((a, b) => a.round - b.round);
  if (!rows.length) {
    return <Empty>No insurer queries on this case.</Empty>;
  }
  return <div className="space-y-4">{rows.map((r) => <div key={r.id} className={focus === r.id ? "rounded-lg ring-2 ring-blue-600" : ""}><Thread qid={r.id} caseId={id} /></div>)}</div>;
}
export default function CaseQueries() { return <Suspense><Tab /></Suspense>; }
