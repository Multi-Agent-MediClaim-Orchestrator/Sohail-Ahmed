"use client";
import Link from "next/link";
import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { QueryRow } from "@/lib/types";
import { Badge, Empty, ErrorCard, Skeleton } from "@/components/ui";
import { useCountdown } from "@/lib/useCountdown";

const TABS: [string, string][] = [["open", "Open"], ["draft_ready", "Draft ready"], ["answered", "Answered"], ["escalated", "Escalated"], ["closed", "Closed"]];

function Due({ q }: { q: QueryRow }) { const c = useCountdown(q.due_by); return <span className={c.overdue ? "font-semibold text-red-800" : ""}>{c.overdue ? "! " : ""}{c.text}</span>; }

function Inbox() {
  const sp = useSearchParams(), router = useRouter();
  const status = sp.get("status") ?? "open";
  const q = useQuery({ queryKey: ["queries", status], queryFn: () => get<{ items: QueryRow[]; counts: Record<string, number> }>(`/v1/queries?status=${status}&limit=50`) });
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Insurer queries</h1>
      <div role="tablist" aria-label="Query status" className="flex gap-1 border-b">{TABS.map(([k, t]) => (
        <button key={k} role="tab" aria-selected={status === k} onClick={() => router.replace(`/queries?status=${k}`)} className={`border-b-2 px-3 py-2 text-sm ${status === k ? "border-blue-700 font-semibold" : "border-transparent"}`}>{t}{q.data?.counts[k] ? ` (${q.data.counts[k]})` : ""}</button>))}</div>
      {q.isLoading ? <Skeleton /> : q.error ? <ErrorCard error={q.error} retry={() => q.refetch()} /> : q.data!.items.length === 0 ? <Empty>No queries here.</Empty> : (
        <div className="overflow-x-auto rounded-lg border bg-white"><table className="w-full text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-700"><tr>{["Claim", "Patient", "Round", "Category", "Due", ""].map((h) => <th key={h} scope="col" className="px-3 py-2">{h}</th>)}</tr></thead>
          <tbody>{q.data!.items.map((r) => (
            <tr key={r.id} className="border-t hover:bg-slate-50">
              <td className="px-3 py-2"><Link className="font-medium text-blue-800 underline" href={`/cases/${r.case_id}/queries?focus=${r.id}`}>{r.claim_ref}</Link></td>
              <td className="px-3 py-2">{r.patient_initials}</td><td className="px-3 py-2">{r.round} of 3</td><td className="px-3 py-2">{r.category.replace(/_/g, " ")}</td>
              <td className="px-3 py-2"><Due q={r} /></td><td className="px-3 py-2">{r.escalation_risk && <Badge tone="warn" icon="!">Two approvers</Badge>}</td>
            </tr>))}</tbody></table></div>)}
    </div>
  );
}
export default function Queries() { return <Suspense><Inbox /></Suspense>; }
