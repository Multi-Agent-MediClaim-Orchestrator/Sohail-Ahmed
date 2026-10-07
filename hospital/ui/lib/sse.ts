"use client";
import { useEffect, useRef, useState } from "react";
import { QueryClient, useQueryClient } from "@tanstack/react-query";

export type Conn = "connecting" | "live" | "reconnecting";
export interface UiEvent { type: string; case_id?: string | null; data?: Record<string, unknown>; ts?: string }

/** What a server event invalidates. Coarse on purpose: refetching is cheap, stale screens are not. */
export function invalidateFor(qc: QueryClient, e: UiEvent) {
  const t = e.type;
  if (e.case_id) qc.invalidateQueries({ queryKey: ["case", e.case_id] });
  if (t.startsWith("case.") || t.startsWith("decision.") || t.startsWith("settlement.")) qc.invalidateQueries({ queryKey: ["cases"] });
  if (t.startsWith("query.")) qc.invalidateQueries({ queryKey: ["queries"] });
  qc.invalidateQueries({ queryKey: ["dashboard"] });
}

export function useEvents(scope: "inbox" | "case", caseId?: string): Conn {
  const qc = useQueryClient();
  const [conn, setConn] = useState<Conn>("connecting");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    const url = `/api/stream?scope=${scope}${caseId ? `&case_id=${caseId}` : ""}`;
    const es = new EventSource(url);
    es.onopen = () => {
      setConn("live");
      // events that happened before this stream connected are not replayed: catch up by refetching what is on screen
      if (caseId) qc.invalidateQueries({ queryKey: ["case", caseId] });
      else qc.invalidateQueries();
    };
    es.onerror = () => setConn("reconnecting");
    const handler = (m: MessageEvent) => {
      try {
        const e = JSON.parse(m.data) as UiEvent;
        if (e.type === "reset") { qc.invalidateQueries(); return; }
        clearTimeout(timer.current);  // coalesce bursts
        timer.current = setTimeout(() => invalidateFor(qc, e), 250);
      } catch { /* ignore malformed frames */ }
    };
    for (const name of ["doc.uploaded", "doc.scanned", "doc.parsed", "doc.classified", "doc.quality", "case.status_changed", "case.created", "completeness.updated",
      "claim.draft_ready", "claim.built", "submission.sent", "submission.failed", "submission.dead", "decision.received", "settlement.received",
      "query.new", "query.updated", "query.draft_ready", "query.overdue", "reminder.due", "reset"]) es.addEventListener(name, handler as EventListener);
    es.onmessage = handler;
    return () => es.close();
  }, [scope, caseId, qc]);
  return conn;
}
