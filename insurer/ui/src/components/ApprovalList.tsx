"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, formatMoney } from "@/lib/api";
import type { ApprovalItem } from "@/lib/types";

export function ApprovalList() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["approvals"], queryFn: () => api<{ items: ApprovalItem[] }>("/v1/approvals/queue") });
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);

  async function vote(id: string, verdict: "approve" | "reject" | "return") {
    const comment = reasons[id]?.trim();
    if (verdict !== "approve" && !comment) return setMsg("A reason is required to reject or return.");
    try {
      await api(`/v1/decisions/${id}/approvals`, { method: "POST", body: { verdict, comment } });
      setMsg(`Recorded: ${verdict}`);
      void qc.invalidateQueries({ queryKey: ["approvals"] });
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Failed");
    }
  }

  return (
    <>
      <h1>Approvals</h1>
      {msg && <p role="status">{msg}</p>}
      {q.data?.items.length === 0 && <p className="muted">Nothing waiting for you.</p>}
      <table>
        <thead><tr><th>Claim</th><th>Amount</th><th>Tier</th><th>Approvals</th><th>Action</th></tr></thead>
        <tbody>
          {q.data?.items.map((a) => (
            <tr key={a.decision_id}>
              <td><Link href={`/cases/${a.case_id}`}>{a.insurer_claim_no}</Link></td><td>{formatMoney(a.amount)}</td><td>{a.tier}</td><td>{a.approvals} of {a.needed}</td>
              <td>
                <input aria-label={`Reason for ${a.insurer_claim_no}`} placeholder="Reason (needed to reject/return)" value={reasons[a.decision_id] ?? ""} onChange={(e) => setReasons({ ...reasons, [a.decision_id]: e.target.value })} />
                <button onClick={() => vote(a.decision_id, "approve")}>Approve</button> <button onClick={() => vote(a.decision_id, "reject")}>Reject</button> <button onClick={() => vote(a.decision_id, "return")}>Return</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
