"use client";
import { ReactNode, useEffect, useRef } from "react";
import { message } from "@/lib/errors";
import { CHIP_TEXT, Chip } from "@/lib/docStatus";

export function Button({ variant = "primary", className = "", ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  const v = { primary: "bg-blue-700 text-white hover:bg-blue-800", secondary: "bg-white border border-slate-300 hover:bg-slate-100", danger: "bg-red-700 text-white hover:bg-red-800", ghost: "hover:bg-slate-100" }[variant];
  return <button {...p} className={`rounded px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${v} ${className}`} />;
}
export function Card({ title, children, actions, className = "" }: { title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white p-4 shadow-sm ${className}`}>
      {(title || actions) && <header className="mb-3 flex items-center justify-between gap-2"><h2 className="font-semibold">{title}</h2><div className="flex gap-2">{actions}</div></header>}
      {children}
    </section>
  );
}
const TONE = { neutral: "bg-slate-100 text-slate-800", good: "bg-green-100 text-green-900", warn: "bg-amber-100 text-amber-900", bad: "bg-red-100 text-red-900", info: "bg-blue-100 text-blue-900" };
export function Badge({ tone = "neutral", children, icon }: { tone?: keyof typeof TONE; children: ReactNode; icon?: string }) {
  return <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TONE[tone]}`}>{icon && <span aria-hidden>{icon}</span>}{children}</span>;
}
export const STATUS_TONE: Record<string, keyof typeof TONE> = {
  draft: "neutral", docs_pending: "warn", docs_complete: "info", building_claim: "info", ready_for_review: "info", submitted: "info", acknowledged: "info",
  under_query: "warn", approved: "good", partially_approved: "warn", rejected: "bad", settled: "good", closed: "neutral",
};
export function StatusBadge({ status }: { status: string }) {
  const icon = { good: "✓", bad: "✕", warn: "!", info: "•", neutral: "·" }[STATUS_TONE[status] ?? "neutral"];
  return <Badge tone={STATUS_TONE[status] ?? "neutral"} icon={icon}>{status.replace(/_/g, " ")}</Badge>;
}
const CHIP_TONE: Record<Chip, keyof typeof TONE> = { scanning: "neutral", quality: "neutral", parsing: "info", classified: "good", needs_review: "warn", failed: "bad", blocked: "bad" };
export function DocChip({ chip }: { chip: Chip }) {
  return <Badge tone={CHIP_TONE[chip]} icon={{ good: "✓", bad: "✕", warn: "!", info: "…", neutral: "…" }[CHIP_TONE[chip]]}>{CHIP_TEXT[chip]}</Badge>;
}
export function ErrorCard({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
      {message(error)} {retry && <button className="ml-2 underline" onClick={retry}>Retry</button>}
    </div>
  );
}
export function Empty({ children }: { children: ReactNode }) { return <p className="py-6 text-center text-sm text-slate-600">{children}</p>; }
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return <div aria-busy="true" className="space-y-2">{Array.from({ length: rows }, (_, i) => <div key={i} className="h-8 animate-pulse rounded bg-slate-200" />)}</div>;
}
export function Field({ label, children, error, hint }: { label: string; children: ReactNode; error?: string; hint?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>{children}
      {hint && <span className="mt-0.5 block text-xs text-slate-600">{hint}</span>}
      {error && <span role="alert" className="mt-0.5 block text-xs text-red-700">{error}</span>}
    </label>
  );
}
export const inputCls = "w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm";

export function Dialog({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("textarea,input,button,select")?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && ref.current) {  // focus trap
        const f = [...ref.current.querySelectorAll<HTMLElement>("button,input,textarea,select,[href]")].filter((x) => !x.hasAttribute("disabled"));
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); prev?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title} className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl">
        <h2 className="mb-3 text-lg font-semibold">{title}</h2>{children}
      </div>
    </div>
  );
}
export function ConfirmDialog({ open, title, body, confirmLabel, danger, onConfirm, onClose, busy }: { open: boolean; title: string; body: ReactNode; confirmLabel: string; danger?: boolean; onConfirm: () => void; onClose: () => void; busy?: boolean }) {
  return (
    <Dialog open={open} title={title} onClose={onClose}>
      <div className="mb-4 text-sm">{body}</div>
      <div className="flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>{confirmLabel}</Button></div>
    </Dialog>
  );
}
export function Toast({ text, tone = "good" }: { text: string | null; tone?: "good" | "bad" }) {
  return <div aria-live="polite" role="status" className="pointer-events-none fixed bottom-4 right-4 z-50">{text && <div className={`rounded px-3 py-2 text-sm text-white shadow ${tone === "good" ? "bg-green-700" : "bg-red-700"}`}>{text}</div>}</div>;
}
