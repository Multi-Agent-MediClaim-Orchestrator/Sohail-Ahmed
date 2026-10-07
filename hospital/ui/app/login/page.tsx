"use client";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button, Card, Field, inputCls } from "@/components/ui";

function Form() {
  const router = useRouter();
  const next = useSearchParams().get("next") ?? "/";
  const [u, setU] = useState(""), [p, setP] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: u, password: p }) });
    setBusy(false);
    if (r.ok) router.push(next.startsWith("/") ? next : "/");
    else setErr((await r.json().catch(() => ({}))).detail ?? "Sign-in failed. Try again.");
  }
  return (
    <main className="mx-auto mt-24 max-w-sm">
      <Card title="Sign in">
        <form onSubmit={submit} className="space-y-3">
          <Field label="User name"><input className={inputCls} value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" required /></Field>
          <Field label="Password" error={err ?? undefined}><input className={inputCls} type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" required /></Field>
          <Button type="submit" disabled={busy} className="w-full">Sign in</Button>
        </form>
        <p className="mt-3 text-xs text-slate-600">Demo accounts: desk1, officer1, officer2, hadmin.</p>
      </Card>
    </main>
  );
}
export default function Login() { return <Suspense><Form /></Suspense>; }
