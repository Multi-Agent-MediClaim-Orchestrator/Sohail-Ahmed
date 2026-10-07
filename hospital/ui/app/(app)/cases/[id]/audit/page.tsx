"use client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, get } from "@/lib/api";
import { message } from "@/lib/errors";
import { Badge, Button, Card, Empty, ErrorCard, Skeleton, inputCls } from "@/components/ui";

interface Ev { seq: number; ts: string; actor_type: string; actor_id: string; event_type: string; payload: Record<string, unknown>; hash: string; prev_hash: string }
interface Page { items: Ev[]; next_after: number | null }
interface Verdict { ok: boolean; checked: number; broken_seq: number | null; reason: string | null }

export default function Audit() {
  const { id } = useParams<{ id: string }>();
  const [type, setType] = useState("");
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const q = useInfiniteQuery({
    queryKey: ["case", id, "audit", type],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => get<Page>(`/v1/audit/${id}?after=${pageParam}&limit=100${type ? `&event_type=${encodeURIComponent(type)}` : ""}`),
    getNextPageParam: (l) => l.next_after ?? undefined,
  });
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  const types = [...new Set(rows.map((r) => r.event_type))].sort();
  async function verify() {
    setBusy(true); setErr(null);
    try { setVerdict(await api<Verdict>("POST", `/v1/audit/${id}/verify`, {})); } catch (e) { setErr(e); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-4">
      <Card title="Audit trail" actions={<>
        <label className="sr-only" htmlFor="etype">Event type</label>
        <select id="etype" className={inputCls + " w-56"} value={type} onChange={(e) => setType(e.target.value)}><option value="">All events</option>{types.map((t) => <option key={t}>{t}</option>)}</select>
        <Button onClick={verify} disabled={busy}>Verify chain</Button></>}>
        <p className="text-xs text-slate-600">Every event is chained to the one before it with a hash; verifying recomputes the whole chain from the database.</p>
        {verdict && (
          <p role="status" className="mt-2">
            {verdict.ok ? <Badge tone="good" icon="✓">Verified: {verdict.checked} events, chain intact</Badge> : <Badge tone="bad" icon="✕">Chain broken at event {verdict.broken_seq}{verdict.reason ? `: ${verdict.reason}` : ""}</Badge>}
          </p>)}
        {err != null && <p role="alert" className="mt-2 text-sm text-red-800">{message(err)}</p>}
      </Card>
      {q.isLoading ? <Skeleton rows={6} /> : q.error ? <ErrorCard error={q.error} retry={() => q.refetch()} /> : rows.length === 0 ? <Empty>No events.</Empty> : (
        <div className="overflow-x-auto rounded-lg border bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-700"><tr>{["#", "Time", "Actor", "Event", "Details"].map((h) => <th key={h} scope="col" className="px-3 py-2">{h}</th>)}</tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.seq} className="border-t align-top">
                <td className="px-3 py-2">{r.seq}</td>
                <td className="px-3 py-2 whitespace-nowrap">{new Date(r.ts).toLocaleString()}</td>
                <td className="px-3 py-2">{r.actor_type} <span className="text-xs text-slate-600">{r.actor_id.length > 18 ? r.actor_id.slice(0, 8) + "…" : r.actor_id}</span></td>
                <td className="px-3 py-2 font-medium">{r.event_type}</td>
                <td className="px-3 py-2"><details><summary className="cursor-pointer text-xs text-blue-800 underline">payload</summary><pre className="mt-1 max-w-xl overflow-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(r.payload, null, 1)}</pre><p className="mt-1 break-all font-mono text-[10px] text-slate-500">hash {r.hash}</p></details></td>
              </tr>))}</tbody>
          </table>
        </div>)}
      {q.hasNextPage && <Button variant="secondary" disabled={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>Load more</Button>}
    </div>
  );
}
