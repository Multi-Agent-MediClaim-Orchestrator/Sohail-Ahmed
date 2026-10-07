"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { WorkspaceView } from "@/components/WorkspaceView";
import { useRoles } from "@/components/Providers";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/useEventStream";
import type { Workspace } from "@/lib/types";

export default function CasePage() {
  const { id } = useParams<{ id: string }>();
  const roles = useRoles();
  const qc = useQueryClient();
  const stream = useEventStream(id);
  const q = useQuery({ queryKey: ["workspace", id], queryFn: () => api<Workspace>(`/v1/cases/${id}/workspace`) });
  if (q.isLoading) return <p>Loading workspace…</p>;
  if (q.error || !q.data) return <p role="alert" className="error">Case not found or you do not have access.</p>;
  return <WorkspaceView ws={q.data} roles={roles} stream={stream} onChanged={() => qc.invalidateQueries({ queryKey: ["workspace", id] })} />;
}
