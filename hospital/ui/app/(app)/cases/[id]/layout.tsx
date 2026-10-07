"use client";
import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { CaseView } from "@/lib/types";
import { Badge, ErrorCard, Skeleton, StatusBadge } from "@/components/ui";
import { useEvents } from "@/lib/sse";
import { useRole } from "@/components/hooks";
import { useCountdown } from "@/lib/useCountdown";

const FLOW = ["draft", "docs_pending", "docs_complete", "ready_for_review", "submitted", "acknowledged", "approved", "settled"];

export default function CaseLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  const path = usePathname();
  const { is } = useRole();
  const conn = useEvents("case", id);
  const q = useQuery({ queryKey: ["case", id], queryFn: () => get<CaseView>(`/v1/cases/${id}`), refetchInterval: (query) => {
      const st = (query.state.data as CaseView | undefined)?.status;
      if (st === "building_claim" || st === "docs_pending") return 3000; // transitional: a missed event must not strand the screen
      return conn === "live" ? false : 10000;
    },
  });
  const intim = useCountdown(q.data?.route?.decision?.intimation_deadline ?? null);
  if (q.isLoading) return <Skeleton rows={3} />;
  if (q.error || !q.data) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const c = q.data;
  const tabs = [["documents", "Documents"], ["checklist", "Checklist"], ["claim", "Claim"], ["submission", "Submission"], ["queries", "Queries"], ["timeline", "Timeline"]];
  const idx = FLOW.indexOf(c.status === "building_claim" ? "docs_complete" : c.status === "under_query" ? "acknowledged" : c.status);
  return (
    <div className="space-y-4">
      <div className="rounded-lg border bg-white p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{c.claim_ref}</h1><StatusBadge status={c.status} />
          <Badge tone="info">{c.route?.decision?.pipeline ?? c.claim_type}</Badge>
          {c.flags.map((f) => <Badge key={f} tone="warn" icon="!">{f.replace(/_/g, " ")}</Badge>)}
          <span className="ml-auto text-xs text-slate-600">{conn === "live" ? "● live" : "○ refreshing"}</span>
        </div>
        <p className="mt-1 text-sm text-slate-700">{c.patient.full_name} · {c.policy.insurer_name} · policy {c.policy.policy_number} · {c.patient.uhid}</p>
        <ol className="mt-3 flex flex-wrap gap-1 text-xs" aria-label="Progress">{FLOW.map((s, i) => <li key={s} aria-current={i === idx ? "step" : undefined} className={`rounded px-2 py-0.5 ${i < idx ? "bg-green-100 text-green-900" : i === idx ? "bg-blue-700 text-white" : "bg-slate-100 text-slate-600"}`}>{s.replace(/_/g, " ")}</li>)}</ol>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-700">
          {c.filing_deadline && <span>Filing deadline {c.filing_deadline}</span>}
          {c.route?.decision?.intimation_deadline && <span className={intim.overdue ? "font-semibold text-red-800" : ""}>Intimate insurer: {intim.text}</span>}
        </div>
        {c.route?.warnings?.map((w) => <p key={w.code} className="mt-2 rounded bg-amber-50 p-2 text-sm text-amber-900">! {w.message}</p>)}
      </div>
      <nav aria-label="Case sections" className="flex gap-1 border-b">
        {tabs.filter(([k]) => k !== "claim" || is("officer") || is("desk")).map(([k, t]) => {
          const active = path.endsWith("/" + k);
          return <Link key={k} href={`/cases/${id}/${k}`} aria-current={active ? "page" : undefined} className={`border-b-2 px-3 py-2 text-sm ${active ? "border-blue-700 font-semibold" : "border-transparent hover:border-slate-300"}`}>{t}</Link>;
        })}
      </nav>
      {children}
    </div>
  );
}
