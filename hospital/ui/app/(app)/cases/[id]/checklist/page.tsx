"use client";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { CaseView, Completeness, CheckItem } from "@/lib/types";
import { Badge, Button, Card, Dialog, Empty, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useAct, useRole, useToast } from "@/components/hooks";

const GROUPS: [string, string, (i: CheckItem) => boolean][] = [
  ["Blockers", "bad", (i) => i.severity === "blocker" && ["missing", "unusable"].includes(i.status)],
  ["Needs attention", "warn", (i) => ["needs_review", "unusable", "pending_processing"].includes(i.status) || (i.status === "missing" && i.severity !== "blocker")],
  ["Satisfied", "good", (i) => i.status === "present_ok"],
  ["Waived or not applicable", "neutral", (i) => ["waived", "not_applicable"].includes(i.status)],
];

export default function Checklist() {
  const { id } = useParams<{ id: string }>();
  const { is } = useRole();
  const [toast, tone, notify] = useToast();
  const [waive, setWaive] = useState<CheckItem | null>(null);
  const [reason, setReason] = useState("");
  const q = useQuery({ queryKey: ["case", id, "checklist"], queryFn: () => get<Completeness>(`/v1/cases/${id}/completeness`) });
  const cs = useQuery({ queryKey: ["case", id], queryFn: () => get<CaseView>(`/v1/cases/${id}`) });
  const act = useAct([["case", id]], notify);
  if (q.isLoading) return <Skeleton rows={5} />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const c = q.data!;
  const seen = new Set<string>();
  return (
    <div className="space-y-4">
      <Card title="Checklist" actions={<>
        <Button variant="secondary" onClick={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/completeness/run`, body: {} })}>Re-check</Button>
        {is("officer") && <Button disabled={!c.complete || act.isPending || cs.data?.status !== "docs_complete"} onClick={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/claim/build`, body: {} }, { onSuccess: () => notify("Building the claim…") })}>Build claim</Button>}</>}>
        <p className="text-sm"><Badge tone={c.complete ? "good" : "warn"} icon={c.complete ? "✓" : "!"}>{c.complete ? "Complete" : "Incomplete"}</Badge>{c.provisional && <span className="ml-2 text-xs text-slate-600">provisional: routing is not final</span>}
          <span className="ml-3 text-xs text-slate-700">{c.summary.blockers} blockers · {c.summary.warnings} warnings · {c.summary.ok} ok</span></p>
      </Card>
      {c.items.length === 0 && <Empty>Nothing to check yet.</Empty>}
      {GROUPS.map(([title, tn, pred]) => {
        const items = c.items.filter((i) => !seen.has(i.rule_id + i.requirement) && pred(i));
        items.forEach((i) => seen.add(i.rule_id + i.requirement));
        if (!items.length) return null;
        return (
          <Card key={title} title={<span className="flex items-center gap-2">{title} <Badge tone={tn as "bad"}>{items.length}</Badge></span>}>
            <ul className="divide-y">{items.map((i) => (
              <li key={i.rule_id + i.requirement} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <span aria-hidden>{{ bad: "✕", warn: "!", good: "✓", neutral: "·" }[tn]}</span>
                <div className="min-w-0 flex-1"><p className="font-medium">{(i.doc_type ?? i.requirement).replace(/_/g, " ")} <span className="font-normal text-slate-600">({i.status.replace(/_/g, " ")})</span></p><p className="text-slate-700">{i.message}</p></div>
                {["missing", "unusable"].includes(i.status) && <Link className="rounded border px-2 py-1 text-xs hover:bg-slate-100" href={`/cases/${id}/documents`}>Upload now</Link>}
                {is("officer") && ["missing", "unusable", "needs_review"].includes(i.status) && i.doc_type && <Button variant="secondary" onClick={() => { setWaive(i); setReason(""); }}>Waive</Button>}
                {i.status === "waived" && is("officer") && i.doc_type && <Button variant="ghost" onClick={() => act.mutate({ method: "DELETE", path: `/v1/cases/${id}/requirements/${i.doc_type}/waive` })}>Undo waiver</Button>}
              </li>))}</ul>
          </Card>);
      })}
      {c.open_doc_requests.length > 0 && (
        <Card title="Open requests"><ul className="divide-y text-sm">{c.open_doc_requests.map((r) => (
          <li key={r.id} className="flex items-center gap-2 py-2"><span className="flex-1">{r.message}<span className="ml-2 text-xs text-slate-600">due {new Date(r.due_by).toLocaleString()} · {r.reminders_sent} reminders</span></span>
            <Button variant="secondary" onClick={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/doc-requests/${r.id}/remind`, body: {} }, { onSuccess: () => notify("Reminder scheduled") })}>Remind</Button></li>))}</ul></Card>)}
      <Dialog open={!!waive} title={`Waive ${(waive?.doc_type ?? waive?.requirement ?? "").replace(/_/g, " ")}`} onClose={() => setWaive(null)}>
        <label className="block text-sm">Reason (at least 20 characters)<textarea className={inputCls + " mt-1"} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <div className="mt-3 flex justify-end gap-2"><Button variant="secondary" onClick={() => setWaive(null)}>Cancel</Button>
          <Button disabled={reason.trim().length < 20 || act.isPending} onClick={() => waive?.doc_type && act.mutate({ method: "POST", path: `/v1/cases/${id}/requirements/${waive.doc_type}/waive`, body: { reason: reason.trim() } }, { onSuccess: () => setWaive(null) })}>Waive</Button></div>
      </Dialog>
      <Toast text={toast} tone={tone} />
    </div>
  );
}
