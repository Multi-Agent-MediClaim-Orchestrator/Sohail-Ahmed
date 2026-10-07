"use client";
import { useCallback, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get } from "@/lib/api";
import { message } from "@/lib/errors";
import type { DocView } from "@/lib/types";
import { DOC_TYPES } from "@/lib/enums";
import { CHIP_HELP, docChip } from "@/lib/docStatus";
import { Badge, Button, Card, ConfirmDialog, DocChip, Empty, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useAct, useRole, useToast } from "@/components/hooks";

const MAX = 25 * 1024 * 1024, OK = ["application/pdf", "image/jpeg", "image/png", "image/tiff"];
interface Up { name: string; pct: number; state: "queued" | "sending" | "done" | "dup" | "error"; text?: string }

function sendOne(caseId: string, file: File, hint: string, onPct: (n: number) => void): Promise<{ ok: boolean; text: string; dup: boolean }> {
  return new Promise((res) => {
    const fd = new FormData();
    fd.append("files", file);
    if (hint) fd.append("doc_type_hint", hint);
    const x = new XMLHttpRequest();
    x.open("POST", `/api/hosp/v1/cases/${caseId}/documents`);
    x.setRequestHeader("Idempotency-Key", crypto.randomUUID());
    x.upload.onprogress = (e) => e.lengthComputable && onPct(Math.round((e.loaded / e.total) * 100));
    x.onload = () => {
      let j: { documents?: { status?: string; error?: { detail?: string } }[]; detail?: string } = {};
      try { j = JSON.parse(x.responseText); } catch { /* not json */ }
      const d = j.documents?.[0];
      if (x.status >= 400 || d?.status === "rejected") res({ ok: false, dup: false, text: d?.error?.detail ?? j.detail ?? `Upload failed (${x.status})` });
      else res({ ok: true, dup: d?.status === "skipped_duplicate", text: "" });
    };
    x.onerror = () => res({ ok: false, dup: false, text: "Network error" });
    x.send(fd);
  });
}

export default function Documents() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const { is } = useRole();
  const [toast, tone, notify] = useToast();
  const [hint, setHint] = useState("");
  const [ups, setUps] = useState<Up[]>([]);
  const [del, setDel] = useState<DocView | null>(null);
  const [drag, setDrag] = useState(false);
  const active = useRef(0);
  const q = useQuery({ queryKey: ["case", id, "documents"], queryFn: () => get<{ documents: DocView[] }>(`/v1/cases/${id}/documents`) });
  const act = useAct([["case", id]], notify);

  const start = useCallback(async (files: File[]) => {
    const rows: Up[] = files.map((f) => ({ name: f.name, pct: 0, state: "queued" }));
    const base = ups.length;
    setUps((u) => [...u, ...rows]);
    const set = (i: number, p: Partial<Up>) => setUps((u) => u.map((x, k) => (k === base + i ? { ...x, ...p } : x)));
    let next = 0;
    const worker = async () => {  // concurrency 3
      while (next < files.length) {
        const i = next++, f = files[i];
        if (f.size > MAX) { set(i, { state: "error", text: "Larger than 25 MB" }); continue; }
        if (!OK.includes(f.type)) { set(i, { state: "error", text: "Only PDF, JPG, PNG or TIFF" }); continue; }
        set(i, { state: "sending" });
        const r = await sendOne(id, f, hint, (pct) => set(i, { pct }));
        set(i, r.ok ? { state: r.dup ? "dup" : "done", pct: 100 } : { state: "error", text: r.text });
      }
    };
    active.current += 1;
    await Promise.all([worker(), worker(), worker()]);
    active.current -= 1;
    qc.invalidateQueries({ queryKey: ["case", id] });
  }, [id, hint, qc, ups.length]);

  const open = async (d: DocView) => {
    try { const r = await api<{ redirect: string }>("GET", `/v1/documents/${d.id}/download`); window.open(r.redirect, "_blank", "noopener"); } catch (e) { notify(message(e), "bad"); }
  };
  return (
    <div className="space-y-4">
      <Card title="Upload documents">
        <div className="mb-2 flex items-center gap-2 text-sm"><label htmlFor="hint">Document type (optional)</label>
          <select id="hint" className={inputCls + " w-56"} value={hint} onChange={(e) => setHint(e.target.value)}><option value="">Detect automatically</option>{DOC_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}</select></div>
        <div onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={(e) => { e.preventDefault(); setDrag(false); void start([...e.dataTransfer.files]); }}
          className={`rounded border-2 border-dashed p-6 text-center text-sm ${drag ? "border-blue-600 bg-blue-50" : "border-slate-300"}`}>
          Drop files here or <label className="cursor-pointer underline">choose files<input type="file" multiple className="sr-only" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff" onChange={(e) => { void start([...(e.target.files ?? [])]); e.target.value = ""; }} /></label>
          <div className="text-xs text-slate-600">PDF, JPG, PNG or TIFF, up to 25 MB each</div>
        </div>
        {ups.length > 0 && <ul className="mt-3 space-y-1 text-sm">{ups.map((u, i) => (
          <li key={i} className="flex items-center gap-2"><span className="w-56 truncate">{u.name}</span>
            {u.state === "sending" && <progress value={u.pct} max={100} className="h-2 w-40" aria-label={`Uploading ${u.name}`} />}
            {u.state === "queued" && <Badge>Waiting</Badge>}{u.state === "done" && <Badge tone="good" icon="✓">Uploaded</Badge>}
            {u.state === "dup" && <Badge tone="info">Already uploaded</Badge>}{u.state === "error" && <Badge tone="bad" icon="✕">{u.text}</Badge>}
          </li>))}</ul>}
      </Card>
      {q.isLoading ? <Skeleton /> : q.error ? <ErrorCard error={q.error} retry={() => q.refetch()} /> : q.data!.documents.length === 0 ? <Empty>No documents yet.</Empty> : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">{q.data!.documents.map((d) => {
          const chip = docChip(d);
          return (
            <Card key={d.id}>
              <div className="flex items-start justify-between gap-2"><div className="min-w-0"><p className="truncate font-medium" title={d.filename}>{d.filename}</p><p className="text-xs text-slate-600">{d.pages ?? "?"} page(s) · {(d.size_bytes / 1024).toFixed(0)} KB</p></div><DocChip chip={chip} /></div>
              {CHIP_HELP[chip] && <p className="mt-1 text-xs text-slate-700">{CHIP_HELP[chip]}</p>}
              {d.quality.flags.length > 0 && <p className="mt-1 flex flex-wrap gap-1">{d.quality.flags.map((f) => <Badge key={f} tone="warn" icon="!">{f.replace(/_/g, " ")}</Badge>)}</p>}
              {d.quality.has_required_stamp === false && <p className="mt-1"><Badge tone="warn" icon="!">Hospital stamp not found</Badge></p>}
              <div className="mt-2 flex items-center gap-2 text-sm"><label className="sr-only" htmlFor={`t-${d.id}`}>Document type</label>
                <select id={`t-${d.id}`} className={inputCls} value={d.doc_type ?? ""} disabled={!(is("desk") || is("officer"))} onChange={(e) => act.mutate({ method: "PATCH", path: `/v1/documents/${d.id}`, body: { doc_type: e.target.value } })}>
                  {d.doc_type === null && <option value="">Choose a type…</option>}{DOC_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}</select>
                {d.classification_confidence !== null && d.doc_type && <span className="text-xs text-slate-600">{Math.round((d.classification_confidence ?? 0) * 100)}%</span>}</div>
              <div className="mt-2 flex gap-2"><Button variant="secondary" onClick={() => open(d)} disabled={chip === "blocked" || chip === "scanning"}>Open</Button>
                <Button variant="secondary" onClick={() => act.mutate({ method: "POST", path: `/v1/documents/${d.id}/reparse`, body: {} })} disabled={chip === "scanning" || chip === "blocked"}>Re-read</Button>
                <Button variant="ghost" onClick={() => setDel(d)}>Delete</Button></div>
            </Card>);
        })}</div>
      )}
      <ConfirmDialog open={!!del} title="Delete this document?" danger confirmLabel="Delete" busy={act.isPending} onClose={() => setDel(null)}
        body={<>This removes <b>{del?.filename}</b> from the case. The checklist is re-checked afterwards.</>}
        onConfirm={() => { if (del) act.mutate({ method: "DELETE", path: `/v1/documents/${del.id}` }, { onSuccess: () => setDel(null) }); }} />
      <Toast text={toast} tone={tone} />
    </div>
  );
}
