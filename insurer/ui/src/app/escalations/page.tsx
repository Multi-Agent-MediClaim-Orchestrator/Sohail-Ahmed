"use client";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import type { EscalationItem } from "@/lib/types";

export default function Escalations() {
  const q = useQuery({ queryKey: ["escalations"], queryFn: () => api<{ items: EscalationItem[] }>("/v1/escalations") });
  return (
    <>
      <h1>Escalations</h1>
      {q.error && <p role="alert" className="error">Could not load (senior reviewer role required).</p>}
      <table>
        <thead><tr><th>Claim</th><th>Reason</th><th>Opened</th><th>Unresolved findings</th></tr></thead>
        <tbody>{q.data?.items.map((e) => <tr key={e.id}><td><Link href={`/cases/${e.case_id}`}>{e.insurer_claim_no}</Link></td><td>{e.reason}</td><td>{new Date(e.opened_at).toLocaleString("en-IN")}</td><td>{e.unresolved}</td></tr>)}</tbody>
      </table>
    </>
  );
}
