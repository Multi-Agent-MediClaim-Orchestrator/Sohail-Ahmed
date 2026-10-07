"use client";
import type { Estimate } from "@/lib/types";
import { inr } from "@/lib/money";
import { Badge, Card } from "@/components/ui";

const TERM_LABEL: Record<string, string> = {
  room_rent_percent: "Room rent limit (% of SI per day)", icu_percent: "ICU limit (% of SI per day)",
  co_pay_percent: "Co-payment (%)", sub_limit: "Procedure sub-limit",
};

/** Admissible-amount estimate from the hospital crew's Policy Estimate step: advice before sign-off, never a gate. */
export function EstimateCard({ e, version }: { e: Estimate | null | undefined; version: number }) {
  if (!e) return null;
  if (e.status !== "estimated") {
    return <Card title="Admissible amount estimate"><p className="text-sm text-slate-700">Not available: {e.reason}</p></Card>;
  }
  return (
    <Card title="Admissible amount estimate" actions={<Badge tone="neutral">{e.product_code} · SI {inr(e.sum_insured ?? "0")}</Badge>}>
      {e.draft_version !== version && <p className="mb-2 text-xs text-amber-900">Made for draft v{e.draft_version}; lines edited since may change it.</p>}
      <dl className="grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
        {[["Claimed", e.claimed_total], ["Eligible", e.eligible_total], ["Estimated payable", e.estimated_payable], ["Patient pays", e.patient_pays]].map(([k, v]) =>
          <div key={k}><dt className="text-xs text-slate-600">{k}</dt><dd className={k === "Estimated payable" ? "font-semibold" : ""}>{inr(v ?? "0")}</dd></div>)}
      </dl>
      <h3 className="mt-3 text-xs font-semibold uppercase text-slate-700">Policy terms used</h3>
      <ul className="mt-1 space-y-1 text-sm">
        {Object.entries(e.terms ?? {}).map(([k, t]) => (
          <li key={k}><span className="font-medium">{TERM_LABEL[k] ?? k}: {k === "sub_limit" ? inr(t.value) : t.value}</span>
            {k === "sub_limit" && e.procedure ? ` (${e.procedure})` : ""} <span className="text-slate-600">“{t.quote}”</span> <Badge tone="neutral">{t.read_by}</Badge></li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-600">Assumes: {(e.assumptions ?? []).join("; ")}. The insurer&apos;s calculation decides the final amount.</p>
    </Card>
  );
}
