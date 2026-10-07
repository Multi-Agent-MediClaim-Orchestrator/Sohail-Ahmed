"use client";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get } from "@/lib/api";
import { message } from "@/lib/errors";
import type { Me } from "@/lib/types";

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => get<Me>("/v1/me"), staleTime: 60000 });
export function useRole() {
  const { data } = useMe();
  const roles = data?.roles ?? [];
  return { me: data, is: (r: string) => roles.includes(r), can: (c: string) => (data?.capabilities ?? []).includes(c) };
}
/** Mutation that reports the API's message through `notify` and invalidates the given key prefixes on success. */
export function useAct(keys: unknown[][], notify: (t: string, tone?: "good" | "bad") => void, ok?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (a: { method: "POST" | "PUT" | "PATCH" | "DELETE"; path: string; body?: unknown; ifMatch?: string }) => api(a.method, a.path, a.body, { ifMatch: a.ifMatch }),
    onSuccess: () => { keys.forEach((k) => qc.invalidateQueries({ queryKey: k })); if (ok) notify(ok); },
    onError: (e) => notify(message(e), "bad"),
  });
}
export function useToast(): [string | null, "good" | "bad", (t: string, tone?: "good" | "bad") => void] {
  const [t, setT] = useState<string | null>(null);
  const [tone, setTone] = useState<"good" | "bad">("good");
  useEffect(() => { if (!t) return; const id = setTimeout(() => setT(null), 4000); return () => clearTimeout(id); }, [t]);
  return [t, tone, (x, tn = "good") => { setT(x); setTone(tn); }];
}
