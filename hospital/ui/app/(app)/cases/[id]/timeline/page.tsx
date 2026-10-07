"use client";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { TimelineEvent } from "@/lib/types";
import { Card, Empty, ErrorCard, Skeleton } from "@/components/ui";

export default function Timeline() {
  const { id } = useParams<{ id: string }>();
  const q = useQuery({ queryKey: ["case", id, "timeline"], queryFn: () => get<{ events: TimelineEvent[] }>(`/v1/cases/${id}/timeline`) });
  if (q.isLoading) return <Skeleton />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const ev = q.data!.events;
  return (
    <Card title="Timeline">
      {ev.length === 0 ? <Empty>No events yet.</Empty> : <ol className="space-y-2 text-sm">{ev.map((e, i) => (
        <li key={i} className="flex gap-3"><time className="w-44 shrink-0 text-xs text-slate-600" dateTime={e.ts}>{new Date(e.ts).toLocaleString()}</time>
          <span>{e.from ? <>{e.from.replace(/_/g, " ")} → </> : null}<b>{(e.to ?? e.kind ?? "event").replace(/_/g, " ")}</b> <span className="text-slate-600">by {e.actor}</span></span></li>))}</ol>}
    </Card>
  );
}
