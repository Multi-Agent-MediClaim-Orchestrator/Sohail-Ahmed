"use client";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

export type StreamState = "connecting" | "live" | "paused";

const EVENTS = ["case.status_changed", "verification.step", "decision.recommended", "approval.requested", "query.sent", "query.answered", "query.triaged", "escalation.raised", "resync"];

/** Server-sent events (audience-filtered by the API). Any case event refreshes that case's data; the browser reconnects with Last-Event-ID by itself. */
export function useEventStream(caseId: string, factory: (url: string) => EventSource = (u) => new EventSource(u)): StreamState {
  const qc = useQueryClient();
  const [state, setState] = useState<StreamState>("connecting");
  useEffect(() => {
    if (typeof window === "undefined") return;
    const es = factory(`/api/v1/events/stream?case_id=${encodeURIComponent(caseId)}`);
    const refresh = () => {
      void qc.invalidateQueries({ queryKey: ["workspace", caseId] });
      void qc.invalidateQueries({ queryKey: ["queries", caseId] });
    };
    es.onopen = () => setState("live");
    es.onerror = () => setState("paused");
    EVENTS.forEach((t) => es.addEventListener(t, refresh));
    es.onmessage = refresh;
    return () => es.close();
  }, [caseId, qc, factory]);
  return state;
}
