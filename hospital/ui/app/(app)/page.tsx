"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { Dashboard } from "@/lib/types";
import { Card, ErrorCard, Skeleton, StatusBadge } from "@/components/ui";
import { useRole } from "@/components/hooks";

const TILES: [string, string][] = [["draft", "Draft"], ["docs_pending", "Documents pending"], ["docs_complete", "Ready to build"], ["ready_for_review", "Ready for review"], ["submitted", "Submitted"], ["under_query", "Under query"], ["approved", "Approved"], ["settled", "Settled"]];

export default function Home() {
  const { is } = useRole();
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => get<Dashboard>("/v1/dashboard/summary"), enabled: is("desk") || is("officer") });
  if (!is("desk") && !is("officer")) return <Card title="Welcome">Use the Config, Users and Outbox pages above.</Card>;
  if (q.isLoading) return <Skeleton rows={4} />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const d = q.data!;
  const attention = d.overdue_queries + d.overdue_requests + d.deadlines.length;
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Dashboard</h1>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {TILES.map(([k, label]) => (
          <Link key={k} href={`/cases?status=${k}`} className="rounded-lg border bg-white p-4 shadow-sm hover:border-blue-600">
            <div className="text-2xl font-semibold">{d.cases_by_status[k] ?? 0}</div><div className="text-sm text-slate-700">{label}</div>
          </Link>
        ))}
      </div>
      <Card title="Needs attention">
        {attention === 0 ? <p className="text-sm text-slate-600">Nothing needs your attention.</p> : (
          <ul className="space-y-1 text-sm">
            {d.overdue_queries > 0 && <li><Link className="underline" href="/queries">{d.overdue_queries} insurer {d.overdue_queries === 1 ? "query is" : "queries are"} overdue</Link></li>}
            {d.overdue_requests > 0 && <li>{d.overdue_requests} document {d.overdue_requests === 1 ? "request is" : "requests are"} past due</li>}
            {d.deadlines.map((x) => <li key={x.case_id}><Link className="underline" href={`/cases/${x.case_id}`}>{x.claim_ref}</Link> filing deadline {x.filing_deadline}</li>)}
          </ul>
        )}
      </Card>
      <Card title="Queries"><div className="flex gap-4 text-sm">{Object.entries(d.queries_by_status).map(([k, v]) => <span key={k}><StatusBadge status={k} /> {v}</span>)}{Object.keys(d.queries_by_status).length === 0 && <span className="text-slate-600">No queries yet.</span>}</div></Card>
    </div>
  );
}
