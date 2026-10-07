"use client";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, formatMoney } from "@/lib/api";
import type { CaseRow } from "@/lib/types";

export default function Cases() {
  const [status, setStatus] = useState("");
  const q = useQuery({ queryKey: ["cases", status], queryFn: () => api<{ items: CaseRow[] }>(`/v1/cases${status ? `?status=${status}` : ""}`) });
  return (
    <>
      <h1>Cases</h1>
      <label>Status
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">all</option>
          {["verifying", "needs_info", "ready_for_decision", "awaiting_approval", "approved", "rejected", "settled"].map((s) => <option key={s}>{s}</option>)}
        </select>
      </label>
      {q.isLoading && <p>Loading…</p>}
      {q.error && <p role="alert" className="error">Could not load cases.</p>}
      <table>
        <thead><tr><th>Claim</th><th>Hospital</th><th>Type</th><th>Status</th><th>Claimed</th><th>SLA</th></tr></thead>
        <tbody>
          {q.data?.items.map((c) => (
            <tr key={c.id}>
              <td><Link href={`/cases/${c.id}`}>{c.insurer_claim_no}</Link></td><td>{c.hospital_name}</td><td>{c.claim_type}</td><td>{c.status}</td>
              <td>{formatMoney(c.claimed_amount)}</td><td>{c.sla_due_at ? new Date(c.sla_due_at).toLocaleDateString("en-IN") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
