"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode } from "react";
import { useRole } from "./hooks";
import { Conn, useEvents } from "@/lib/sse";

const DOT: Record<Conn, string> = { live: "bg-green-600", connecting: "bg-slate-400", reconnecting: "bg-amber-500" };
const LABEL: Record<Conn, string> = { live: "Live", connecting: "Connecting", reconnecting: "Reconnecting (refreshing every 10 s)" };

export function Shell({ children }: { children: ReactNode }) {
  const { me, is } = useRole();
  const path = usePathname();
  const router = useRouter();
  const conn = useEvents("inbox");
  const staff = is("desk") || is("officer");
  const links = [
    ...(staff ? [["/", "Dashboard"], ["/cases", "Cases"], ["/queries", "Queries"]] : []),
    ...(is("admin") ? [["/admin/config/doc_requirements", "Config"], ["/admin/users", "Users"], ["/admin/outbox", "Outbox"]] : []),
  ];
  return (
    <div className="min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:bg-white focus:p-2">Skip to content</a>
      <header className="border-b bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-2">
          <span className="font-semibold">Hospital claims</span>
          <nav aria-label="Main" className="flex gap-4 text-sm">
            {links.map(([h, t]) => <Link key={h} href={h} aria-current={path === h || (h !== "/" && path.startsWith(h.split("/").slice(0, 2).join("/"))) ? "page" : undefined} className="py-1 hover:underline aria-[current=page]:font-semibold aria-[current=page]:underline">{t}</Link>)}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="flex items-center gap-1" title={LABEL[conn]}><span className={`h-2 w-2 rounded-full ${DOT[conn]}`} aria-hidden /><span className="text-xs text-slate-600">{LABEL[conn].split(" (")[0]}</span></span>
            <span className="text-slate-700">{me?.name} <span className="text-xs text-slate-500">({me?.roles.filter((r) => ["desk", "officer", "admin"].includes(r)).join(", ")})</span></span>
            <button className="underline" onClick={async () => { await fetch("/api/auth/logout", { method: "POST" }); router.push("/login"); }}>Sign out</button>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-5">{children}</main>
    </div>
  );
}
