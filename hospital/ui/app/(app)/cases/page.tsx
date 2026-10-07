"use client";
import Link from "next/link";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useInfiniteQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { CaseList } from "@/lib/types";
import { Badge, Button, Empty, ErrorCard, Skeleton, StatusBadge, inputCls } from "@/components/ui";
import { inr } from "@/lib/money";

const STATUSES = ["", "draft", "docs_pending", "docs_complete", "building_claim", "ready_for_review", "submitted", "acknowledged", "under_query", "approved", "partially_approved", "rejected", "settled", "closed"];

function List() {
  const sp = useSearchParams(), router = useRouter();
  const status = sp.get("status") ?? "", qtext = sp.get("q") ?? "", ctype = sp.get("claim_type") ?? "";
  const [text, setText] = useState(qtext);
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp.toString()); v ? n.set(k, v) : n.delete(k); router.replace(`/cases?${n}`); };
  const q = useInfiniteQuery({
    queryKey: ["cases", status, qtext, ctype],
    initialPageParam: "" as string,
    queryFn: ({ pageParam }) => get<CaseList>(`/v1/cases?limit=25${status ? `&status=${status}` : ""}${qtext ? `&q=${encodeURIComponent(qtext)}` : ""}${ctype ? `&claim_type=${ctype}` : ""}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    getNextPageParam: (l) => l.next_cursor ?? undefined,
  });
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between"><h1 className="text-xl font-semibold">Cases</h1><Link href="/cases/new" className="rounded bg-blue-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-800">New case</Link></div>
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); set("q", text); }} role="search">
        <select aria-label="Status" className={inputCls + " w-48"} value={status} onChange={(e) => set("status", e.target.value)}>{STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace(/_/g, " ") : "All statuses"}</option>)}</select>
        <select aria-label="Claim type" className={inputCls + " w-44"} value={ctype} onChange={(e) => set("claim_type", e.target.value)}><option value="">All types</option><option value="cashless">Cashless</option><option value="reimbursement">Reimbursement</option></select>
        <input aria-label="Search" placeholder="Claim ref or patient" className={inputCls + " w-64"} value={text} onChange={(e) => setText(e.target.value)} />
        <Button variant="secondary" type="submit">Search</Button>
      </form>
      {q.isLoading ? <Skeleton rows={6} /> : q.error ? <ErrorCard error={q.error} retry={() => q.refetch()} /> : rows.length === 0 ? <Empty>No cases match.</Empty> : (
        <div className="overflow-x-auto rounded-lg border bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-700"><tr>{["Claim", "Patient", "Type", "Status", "Flags", "Amount", "Updated"].map((h) => <th key={h} scope="col" className="px-3 py-2">{h}</th>)}</tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id} className="border-t hover:bg-slate-50">
                <td className="px-3 py-2"><Link className="font-medium text-blue-800 underline" href={`/cases/${r.id}/documents`}>{r.claim_ref}</Link></td>
                <td className="px-3 py-2">{r.patient_name}</td><td className="px-3 py-2">{r.claim_type}</td>
                <td className="px-3 py-2"><StatusBadge status={r.status} /></td>
                <td className="px-3 py-2">{r.flags.map((f) => <Badge key={f} tone="warn">{f.replace(/_/g, " ")}</Badge>)}</td>
                <td className="px-3 py-2">{inr(r.claimed_amount)}</td><td className="px-3 py-2">{new Date(r.updated_at).toLocaleString()}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
      {q.hasNextPage && <Button variant="secondary" disabled={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>Load more</Button>}
    </div>
  );
}
export default function Cases() { return <Suspense><List /></Suspense>; }
