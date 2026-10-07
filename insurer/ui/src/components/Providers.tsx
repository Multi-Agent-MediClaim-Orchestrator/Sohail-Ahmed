"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

export function readRolesCookie(): string[] {
  if (typeof document === "undefined") return [];
  const m = document.cookie.split("; ").find((c) => c.startsWith("ins_roles="));
  return m ? decodeURIComponent(m.split("=")[1]).split(",").filter(Boolean) : [];
}

export function useRoles(): string[] {
  const [roles, setRoles] = useState<string[]>([]);
  useEffect(() => setRoles(readRolesCookie()), []);
  return roles;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5_000 } } }));
  const [ready, setReady] = useState(process.env.NEXT_PUBLIC_MOCK !== "1");
  useEffect(() => {
    if (process.env.NEXT_PUBLIC_MOCK === "1") {
      void import("@/mocks/browser").then(({ worker }) => worker.start({ onUnhandledRequest: "bypass" })).then(() => setReady(true));
    }
  }, []);
  if (!ready) return <p>Starting mock API…</p>;
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}
