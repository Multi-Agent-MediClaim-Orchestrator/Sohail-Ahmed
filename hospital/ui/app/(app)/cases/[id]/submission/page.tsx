"use client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import type { CaseView, Submission } from "@/lib/types";
import { Badge, Button, Card, ConfirmDialog, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useAct, useRole, useToast } from "@/components/hooks";

export default function SubmissionTab() {
  const { id } = useParams<{ id: string }>();
  const { is } = useRole();
  const [toast, tone, notify] = useToast();
  const [wd, setWd] = useState(false);
  const [reason, setReason] = useState("patient_requested");
  const q = useQuery({ queryKey: ["case", id, "submission"], queryFn: () => get<Submission>(`/v1/cases/${id}/submission`) });
  const cs = useQuery({ queryKey: ["case", id], queryFn: () => get<CaseView>(`/v1/cases/${id}`) });
  const act = useAct([["case", id]], notify);
  if (q.isLoading) return <Skeleton />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  const s = q.data!;
  const steps = [["Queued", !!s.status], ["Sent", s.status === "sent" || !!s.acknowledged_at], ["Acknowledged", !!s.acknowledged_at]] as const;
  return (
    <div className="space-y-4">
      <Card title="Submission">
        <ol className="flex gap-2 text-sm" aria-label="Delivery">{steps.map(([t, done]) => <li key={t} className={`rounded px-2 py-1 ${done ? "bg-green-100 text-green-900" : "bg-slate-100 text-slate-600"}`}>{done ? "✓ " : "· "}{t}</li>)}</ol>
        <dl className="mt-3 grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
          <div><dt className="text-xs text-slate-600">Delivery status</dt><dd>{s.status ?? "not submitted"}</dd></div>
          <div><dt className="text-xs text-slate-600">Attempts</dt><dd>{s.attempts}</dd></div>
          <div><dt className="text-xs text-slate-600">Insurer claim no.</dt><dd>{s.insurer_claim_no ?? "—"}</dd></div>
          <div><dt className="text-xs text-slate-600">Acknowledged</dt><dd>{s.acknowledged_at ? new Date(s.acknowledged_at).toLocaleString() : "—"}</dd></div>
        </dl>
        {s.last_error && <p role="alert" className="mt-3 rounded bg-red-50 p-2 text-sm text-red-900">Last error: {s.last_error}</p>}
        {s.status && !s.acknowledged_at && <p className="mt-2"><Badge tone="warn" icon="!">Waiting for the insurer to acknowledge</Badge></p>}
        <div className="mt-3 flex gap-2">
          {is("officer") && (s.status === "dead" || s.status === "failed") && <Button onClick={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/submission/retry`, body: {} }, { onSuccess: () => notify("Retrying delivery") })}>Retry now</Button>}
          {is("officer") && s.acknowledged_at && !["settled", "closed", "rejected"].includes(cs.data?.status ?? "") && <Button variant="danger" onClick={() => setWd(true)}>Withdraw claim</Button>}
        </div>
      </Card>
      <ConfirmDialog open={wd} title="Withdraw this claim?" danger confirmLabel="Withdraw" busy={act.isPending} onClose={() => setWd(false)}
        body={<><p>The insurer is told the claim is withdrawn. This cannot be undone.</p><label className="mt-2 block">Reason<select className={inputCls} value={reason} onChange={(e) => setReason(e.target.value)}><option value="patient_requested">Patient requested</option><option value="duplicate">Duplicate</option><option value="hospital_error">Hospital error</option><option value="other">Other</option></select></label></>}
        onConfirm={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/claim/withdraw`, body: { reason } }, { onSuccess: () => setWd(false) })} />
      <Toast text={toast} tone={tone} />
    </div>
  );
}
