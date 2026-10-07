"use client";
import { useQuery } from "@tanstack/react-query";
import { get } from "@/lib/api";
import { Badge, Button, Card, ErrorCard, Skeleton, Toast } from "@/components/ui";
import { useAct, useToast } from "@/components/hooks";

interface U { id: string; email: string; display_name?: string; name?: string; role: string; active: boolean; last_login_at?: string | null }
export default function Users() {
  const [toast, tone, notify] = useToast();
  const q = useQuery({ queryKey: ["admin", "users"], queryFn: () => get<{ items: U[] }>("/v1/admin/users?size=100") });
  const act = useAct([["admin", "users"]], notify, "Saved");
  if (q.isLoading) return <Skeleton />;
  if (q.error) return <ErrorCard error={q.error} retry={() => q.refetch()} />;
  return (
    <Card title="Users"><p className="mb-2 text-xs text-slate-600">Roles are managed in Keycloak; here you can switch an account on or off.</p>
      <table className="w-full text-left text-sm"><thead className="text-xs uppercase text-slate-700"><tr>{["Name", "Email", "Role", "Status", ""].map((h) => <th key={h} scope="col" className="px-2 py-1">{h}</th>)}</tr></thead>
        <tbody>{q.data!.items.map((u) => (
          <tr key={u.id} className="border-t"><td className="px-2 py-1">{u.display_name ?? u.name}</td><td className="px-2">{u.email}</td><td className="px-2">{u.role}</td>
            <td className="px-2"><Badge tone={u.active ? "good" : "bad"} icon={u.active ? "✓" : "✕"}>{u.active ? "Active" : "Disabled"}</Badge></td>
            <td className="px-2"><Button variant="secondary" onClick={() => act.mutate({ method: "PATCH", path: `/v1/admin/users/${u.id}`, body: { active: !u.active } })}>{u.active ? "Deactivate" : "Activate"}</Button></td></tr>))}</tbody></table>
      <Toast text={toast} tone={tone} />
    </Card>
  );
}
