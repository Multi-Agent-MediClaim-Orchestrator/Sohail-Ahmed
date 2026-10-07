"use client";
import { useState } from "react";
import type { DocItem, Finding } from "@/lib/types";

const COLOR: Record<string, string> = { blocker: "var(--blocker)", warning: "var(--warning)", info: "var(--info)" };

/** Document list + viewer (image or PDF; auth rides on the BFF cookie) with evidence rectangles (bbox normalised 0..1) coloured by severity. */
export function DocumentViewer({ caseId, docs, findings, selected, onSelect, onFindingClick }: {
  caseId: string; docs: DocItem[]; findings: Finding[]; selected: string | null; onSelect: (id: string) => void; onFindingClick?: (code: string) => void;
}) {
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(100);
  const doc = docs.find((d) => d.id === selected) ?? null;
  const url = doc ? `/api/v1/cases/${caseId}/documents/${doc.id}/download` : null;
  const overlays = doc ? findings.flatMap((f) => (f.evidence ?? []).filter((e) => e.doc_id === doc.id && e.bbox && (e.page ?? 1) === page).map((e) => ({ f, e }))) : [];
  const isImage = !!doc && /\.(png|jpe?g)$/i.test(doc.filename);
  return (
    <div>
      <ul aria-label="Document list">
        {docs.map((d) => (
          <li key={d.id}>
            <button aria-pressed={d.id === selected} onClick={() => { onSelect(d.id); setPage(1); }}>
              {d.doc_type.replace(/_/g, " ")} · {d.filename}
            </button>
          </li>
        ))}
      </ul>
      {doc ? (
        <div aria-label="Document viewer">
          <div>
            <button aria-label="Previous page" disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button> page {page} / {doc.pages ?? "?"}{" "}
            <button aria-label="Next page" disabled={doc.pages !== null && page >= doc.pages} onClick={() => setPage(page + 1)}>›</button>{" "}
            <button aria-label="Zoom out" onClick={() => setZoom(Math.max(50, zoom - 25))}>−</button> {zoom}%{" "}
            <button aria-label="Zoom in" onClick={() => setZoom(Math.min(300, zoom + 25))}>+</button>
          </div>
          <div style={{ position: "relative", width: `${zoom}%` }}>
            {isImage ? <img src={url!} alt={doc.filename} style={{ width: "100%" }} /> : <iframe title={doc.filename} src={`${url}#page=${page}`} style={{ width: "100%", height: 480, border: "1px solid var(--line)" }} />}
            {overlays.map(({ f, e }, i) => (
              <button key={i} aria-label={`Evidence ${f.code}`} onClick={() => onFindingClick?.(f.code)}
                style={{ position: "absolute", left: `${e.bbox![0] * 100}%`, top: `${e.bbox![1] * 100}%`, width: `${(e.bbox![2] - e.bbox![0]) * 100}%`, height: `${(e.bbox![3] - e.bbox![1]) * 100}%`,
                  border: `2px solid ${COLOR[f.severity]}`, background: "transparent", padding: 0 }} />
            ))}
          </div>
        </div>
      ) : <p className="muted">Select a document.</p>}
    </div>
  );
}
