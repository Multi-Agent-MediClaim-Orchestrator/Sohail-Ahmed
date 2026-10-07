"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import type { ConfigVersion } from "@/lib/types";

export default function Admin() {
  const domains = useQuery({ queryKey: ["domains"], queryFn: () => api<{ domains: string[] }>("/v1/admin/config/domains") });
  const [domain, setDomain] = useState<string | null>(null);
  const names = useQuery({ queryKey: ["names", domain], enabled: !!domain, queryFn: () => api<{ names: string[] }>(`/v1/admin/config/${domain}`) });
  const [name, setName] = useState<string | null>(null);
  const versions = useQuery({ queryKey: ["versions", domain, name], enabled: !!domain && !!name, queryFn: () => api<{ items: ConfigVersion[] }>(`/v1/admin/config/${domain}/${name}/versions`) });
  const [msg, setMsg] = useState<string | null>(null);

  async function act(version: number, action: "validate" | "dry-run" | "publish") {
    try {
      const r = await api<Record<string, unknown>>(`/v1/admin/config/${domain}/${name}/versions/${version}:${action}`, { method: "POST", body: {} });
      setMsg(`${action} v${version}: ${JSON.stringify(r).slice(0, 200)}`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "failed");
    }
  }

  return (
    <>
      <h1>Configuration</h1>
      <p className="muted">Versioned rows: draft → validate → dry-run → publish. Thresholds, policy rules and exclusions need a second approver (enforced by the server).</p>
      <div style={{ display: "flex", gap: 24 }}>
        <ul aria-label="Domains">{domains.data?.domains.map((d) => <li key={d}><button aria-pressed={d === domain} onClick={() => { setDomain(d); setName(null); }}>{d}</button></li>)}</ul>
        <ul aria-label="Config sets">{names.data?.names.map((n) => <li key={n}><button aria-pressed={n === name} onClick={() => setName(n)}>{n}</button></li>)}</ul>
        <table aria-label="Versions">
          <thead><tr><th>Version</th><th>Status</th><th>Note</th><th /></tr></thead>
          <tbody>
            {versions.data?.items.map((v) => (
              <tr key={v.version}>
                <td>v{v.version}</td><td>{v.status}</td><td>{v.change_note}</td>
                <td>{v.status === "draft" && <><button onClick={() => act(v.version, "validate")}>Validate</button> <button onClick={() => act(v.version, "dry-run")}>Dry-run</button> <button onClick={() => act(v.version, "publish")}>Publish</button></>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {msg && <p role="status">{msg}</p>}
    </>
  );
}
