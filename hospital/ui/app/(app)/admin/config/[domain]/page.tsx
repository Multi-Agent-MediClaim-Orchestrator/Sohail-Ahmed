"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, get } from "@/lib/api";
import { message } from "@/lib/errors";
import { Badge, Button, Card, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useToast } from "@/components/hooks";

interface Ver { version: number; status: string; effective_from: string | null; change_note: string; created_by: string; published_by: string | null; second_approver: string | null }
interface VerFull extends Ver { payload: unknown }
const DOMAINS = ["doc_requirements", "deadlines", "router_rules", "confidence_gates"];

export default function ConfigPage() {
  const { domain } = useParams<{ domain: string }>();
  const [toast, tone, notify] = useToast();
  const [sel, setSel] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [out, setOut] = useState<unknown>(null);
  const list = useQuery({ queryKey: ["admin", "config", domain], queryFn: () => get<{ versions: Ver[] }>(`/v1/admin/config/${domain}`) });
  const one = useQuery({ queryKey: ["admin", "config", domain, sel], enabled: sel !== null, queryFn: () => get<VerFull>(`/v1/admin/config/${domain}/${sel}`) });
  useEffect(() => { if (list.data && sel === null && list.data.versions.length) setSel(list.data.versions.reduce((a, b) => (b.version > a.version ? b : a)).version); }, [list.data, sel]);
  useEffect(() => { if (one.data) { setText(JSON.stringify(one.data.payload, null, 2)); setOut(null); } }, [one.data]);
  const run = async (label: string, fn: () => Promise<unknown>, show = false) => {
    try { const r = await fn(); if (show) setOut(r); notify(label); await list.refetch(); await one.refetch(); } catch (e) { notify(message(e), "bad"); }
  };
  let parsed: unknown = null, parseErr: string | null = null;
  try { parsed = JSON.parse(text || "null"); } catch (e) { parseErr = (e as Error).message; }
  const v = one.data;
  return (
    <div className="space-y-4">
      <nav aria-label="Config domains" className="flex gap-2 text-sm">{DOMAINS.map((d) => <Link key={d} href={`/admin/config/${d}`} aria-current={d === domain ? "page" : undefined} className="rounded px-2 py-1 hover:bg-slate-100 aria-[current=page]:bg-blue-100 aria-[current=page]:font-semibold">{d.replace(/_/g, " ")}</Link>)}</nav>
      {list.isLoading ? <Skeleton /> : list.error ? <ErrorCard error={list.error} retry={() => list.refetch()} /> : (
        <div className="grid gap-4 lg:grid-cols-4">
          <Card title="Versions"><ul className="space-y-1 text-sm">{list.data!.versions.map((x) => (
            <li key={x.version}><button onClick={() => setSel(x.version)} aria-current={sel === x.version} className={`w-full rounded px-2 py-1 text-left hover:bg-slate-100 ${sel === x.version ? "bg-blue-50 font-semibold" : ""}`}>v{x.version} <Badge tone={x.status === "published" ? "good" : x.status === "draft" ? "warn" : "neutral"}>{x.status}</Badge><span className="block text-xs text-slate-600">{x.change_note}</span></button></li>))}</ul></Card>
          <div className="space-y-3 lg:col-span-3">
            {v && <Card title={`v${v.version} · ${v.status}`} actions={<>
              <Button variant="secondary" onClick={() => run("Created a draft from this version", () => api("POST", `/v1/admin/config/${domain}`, { payload: parsed, change_note: note || `copy of v${v.version}` }))} disabled={!!parseErr}>Save as new draft</Button>
              <Button variant="secondary" onClick={() => run("Validated", () => api("POST", `/v1/admin/config/${domain}/${v.version}/validate`, {}), true)}>Validate</Button>
              <Button variant="secondary" onClick={() => run("Dry run finished", () => api("POST", `/v1/admin/config/${domain}/${v.version}/dry-run`, {}), true)}>Dry-run</Button>
              {v.status === "draft" && <Button onClick={() => run("Publish requested", () => api("POST", `/v1/admin/config/${domain}/${v.version}/publish`, {}), true)}>Publish</Button>}
              {v.status === "published" && <Button variant="danger" onClick={() => run("Retired", () => api("POST", `/v1/admin/config/${domain}/${v.version}/retire`, {}))}>Retire</Button>}</>}>
              {v.second_approver === null && v.status === "draft" && <p className="mb-2 text-xs text-slate-700">Publishing needs two different admins: the first request records you, a second admin completes it.</p>}
              <label className="mb-2 block text-sm">Change note<input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} /></label>
              <label className="block text-sm">Configuration (JSON)<textarea className={inputCls + " font-mono text-xs"} rows={18} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-invalid={!!parseErr} /></label>
              {parseErr && <p role="alert" className="text-xs text-red-800">Not valid JSON: {parseErr}</p>}
              <p className="mt-1 text-xs text-slate-600">Published versions are read-only: edit and use “Save as new draft”.</p>
            </Card>}
            {out !== null && <Card title="Result"><pre className="max-h-80 overflow-auto text-xs">{JSON.stringify(out, null, 2)}</pre></Card>}
          </div>
        </div>)}
      <Toast text={toast} tone={tone} />
    </div>
  );
}
