"use client";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import { Badge, Button, Dialog } from "@/components/ui";

interface ParseDetail { passes: { pass_no: number; engine: string; confidence: number | null; typed_json: Record<string, unknown> }[]; agreement_score: number | null; fields_disagreeing: string[] }
export interface ViewerDoc { id: string; filename: string; pages: number | null; doc_type: string | null }

const FIELD_LABEL: Record<string, string> = { patient_name: "Patient", date: "Date", total: "Total", discounts: "Discounts", bill_number: "Bill no.", admitted_on: "Admitted", discharged_on: "Discharged", doctor_name: "Doctor", diagnosis: "Diagnosis", icd_codes: "ICD-10", medicines: "Medicines", hospital_name: "Hospital" };
const show = (v: unknown) => (v === null || v === undefined ? "—" : Array.isArray(v) ? v.map(String).join(", ") : String(v));

/** Page images (rendered by the API on first view), zoom, rotate, and what the pipeline read from the document. */
export function DocViewer({ doc, page = 1, onClose }: { doc: ViewerDoc | null; page?: number; onClose: () => void }) {
  const [n, setN] = useState(page);
  const [zoom, setZoom] = useState(1);
  const [rot, setRot] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setN(page); setZoom(1); setRot(0); setFailed(false); }, [doc?.id, page]);
  const parse = useQuery({ queryKey: ["doc", doc?.id, "parse"], enabled: !!doc, queryFn: () => get<ParseDetail>(`/v1/documents/${doc!.id}/parse`) });
  const total = doc?.pages ?? 1;
  const first = parse.data?.passes[0]?.typed_json ?? {};
  const rows = Object.keys(FIELD_LABEL).filter((k) => k in first);
  return (
    <Dialog open={!!doc} title={doc ? `${doc.filename}` : ""} onClose={onClose} wide>
      {doc && (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="md:col-span-2">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
              <Button variant="secondary" disabled={n <= 1} onClick={() => { setN(n - 1); setFailed(false); }} aria-label="Previous page">‹</Button>
              <span aria-live="polite">Page {n} of {total}</span>
              <Button variant="secondary" disabled={n >= total} onClick={() => { setN(n + 1); setFailed(false); }} aria-label="Next page">›</Button>
              <span className="mx-2 text-slate-400" aria-hidden>|</span>
              <Button variant="secondary" onClick={() => setZoom(Math.max(0.5, zoom - 0.25))} aria-label="Zoom out">−</Button>
              <span>{Math.round(zoom * 100)}%</span>
              <Button variant="secondary" onClick={() => setZoom(Math.min(3, zoom + 0.25))} aria-label="Zoom in">+</Button>
              <Button variant="secondary" onClick={() => setRot((rot + 90) % 360)} aria-label="Rotate">⟳</Button>
            </div>
            <div tabIndex={0} role="region" aria-label={`Page ${n} image (scrollable)`} className="max-h-[65vh] overflow-auto rounded border bg-slate-100 p-2">
              {failed ? <p className="p-6 text-sm text-slate-700">No preview for this page.</p> : (
                // eslint-disable-next-line @next/next/no-img-element
                <img key={`${doc.id}-${n}`} src={`/api/hosp/v1/documents/${doc.id}/pages/${n}`} alt={`${doc.filename}, page ${n}`} onError={() => setFailed(true)}
                  style={{ width: `${zoom * 100}%`, transform: `rotate(${rot}deg)`, transformOrigin: "center", maxWidth: "none" }} />
              )}
            </div>
          </div>
          <aside aria-label="Extracted values">
            <h3 className="mb-2 font-semibold">What we read</h3>
            {parse.isLoading ? <p className="text-sm text-slate-600">Loading…</p> : rows.length === 0 ? <p className="text-sm text-slate-600">Nothing extracted yet.</p> : (
              <dl className="space-y-1 text-sm">
                {rows.map((k) => (
                  <div key={k}><dt className="text-xs text-slate-600">{FIELD_LABEL[k]}{parse.data?.fields_disagreeing.includes(k) && <> <Badge tone="warn" icon="!">passes differ</Badge></>}</dt><dd>{show(first[k])}</dd></div>
                ))}
              </dl>
            )}
            {parse.data && <p className="mt-3 text-xs text-slate-600">{parse.data.passes.length} reading pass(es){parse.data.agreement_score !== null && ` · agreement ${Math.round(parse.data.agreement_score * 100)}%`}{parse.data.passes[0]?.confidence != null && ` · reader confidence ${Math.round((parse.data.passes[0].confidence ?? 0) * 100)}%`}</p>}
          </aside>
        </div>
      )}
      <div className="mt-3 flex justify-end"><Button variant="secondary" onClick={onClose}>Close</Button></div>
    </Dialog>
  );
}
