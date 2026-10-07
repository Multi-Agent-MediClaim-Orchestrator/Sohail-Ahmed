"use client";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import { Badge, Card, Empty, ErrorCard, Skeleton } from "@/components/ui";

interface Row { claim_ref: string; kind: string; status: string; attempts: number; last_error: string | null; created_at: string }
export default function Outbox() {
  const q = useQuery({ queryKey: ["admin", "outbox"], queryFn: () => get<{ items: Row[] }>("/v1/admin/outbox"), refetchInterval: 15000 });
  if (q.isLoading) return <Skeleton />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  return (
    <Card title="Outbox: messages to the insurer">
      {q.data!.items.length === 0 ? <Empty>Nothing waiting or failed.</Empty> : (
        <table className="w-full text-left text-sm"><thead className="text-xs uppercase text-slate-700"><tr>{["Claim", "Message", "Status", "Attempts", "Last error"].map((h) => <th key={h} scope="col" className="px-2 py-1">{h}</th>)}</tr></thead>
          <tbody>{q.data!.items.map((r, i) => <tr key={i} className="border-t"><td className="px-2 py-1">{r.claim_ref}</td><td className="px-2">{r.kind}</td><td className="px-2"><Badge tone={r.status === "dead" ? "bad" : r.status === "sent" ? "good" : "warn"}>{r.status}</Badge></td><td className="px-2">{r.attempts}</td><td className="px-2 text-xs">{r.last_error ?? "—"}</td></tr>)}</tbody></table>)}
      <p className="mt-3 text-xs text-slate-600">To retry a dead message, open the case Submission tab as an officer.</p>
    </Card>
  );
}
