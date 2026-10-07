"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatMoney } from "@/lib/api";
import type { SettlementRow } from "@/lib/types";

export default function Settlements() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settlements"], queryFn: () => api<{ items: SettlementRow[] }>("/v1/settlements") });
  return (
    <>
      <h1>Settlements</h1>
      <p className="muted">Simulation only: no real money moves (accounts are SIM-prefixed, mode is sim).</p>
      <table>
        <thead><tr><th>Id</th><th>Amount</th><th>Status</th><th>UTR</th><th /></tr></thead>
        <tbody>
          {q.data?.items.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td><td>{formatMoney(s.amount)}</td><td>{s.status}</td><td>{s.utr ?? "—"}</td>
              <td>{s.status === "failed" && <button onClick={async () => { await api(`/v1/settlements/${s.id}/retry`, { method: "POST", body: {} }); void qc.invalidateQueries({ queryKey: ["settlements"] }); }}>Retry</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
