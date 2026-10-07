"use client";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, get, PatchOp } from "@/lib/api";
import { ApiError, message } from "@/lib/errors";
import type { BillLine, CaseView, ClaimView, DocView, Finding } from "@/lib/types";
import { CATEGORIES } from "@/lib/enums";
import { inr, isAmount, minus, sumAmounts } from "@/lib/money";
import { Badge, Button, Card, ConfirmDialog, Dialog, Empty, ErrorCard, Skeleton, Toast, inputCls } from "@/components/ui";
import { useAct, useRole, useToast } from "@/components/hooks";
import { DocViewer, ViewerDoc } from "@/components/DocViewer";

export default function Claim() {
  const { id } = useParams<{ id: string }>();
  const { is } = useRole();
  const [toast, tone, notify] = useToast();
  const cs = useQuery({ queryKey: ["case", id], queryFn: () => get<CaseView>(`/v1/cases/${id}`) });
  const q = useQuery({ queryKey: ["case", id, "claim"], queryFn: () => get<ClaimView>(`/v1/cases/${id}/claim`), retry: false });
  const docs = useQuery({ queryKey: ["case", id, "documents"], queryFn: () => get<{ documents: DocView[] }>(`/v1/cases/${id}/documents`) });
  const act = useAct([["case", id]], notify);
  const [lines, setLines] = useState<BillLine[]>([]);
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [ack, setAck] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [confirm, setConfirm] = useState<"signoff" | "submit" | "return" | null>(null);
  const [late, setLate] = useState("");
  const [view, setView] = useState<{ doc: ViewerDoc; page: number } | null>(null);
  useEffect(() => { if (q.data && !dirty) setLines(q.data.payload.bill_lines); }, [q.data, dirty]);

  if (q.isLoading) return <Skeleton rows={5} />;
  const status = cs.data?.status;
  if (q.error) {
    const nf = q.error instanceof ApiError && q.error.problem.status === 404;
    return nf ? <Empty>{status === "building_claim" ? "The claim is being assembled…" : "No claim draft yet. Build it from the checklist once documents are complete."}</Empty> : <ErrorCard error={q.error} retry={() => q.refetch()} />;
  }
  const c = q.data!;
  const editable = is("officer") && ["ready_for_review"].includes(status ?? "") && !c.signoff;
  const warnings: Finding[] = c.validation.warnings ?? [];
  const errors: Finding[] = c.validation.errors ?? [];
  const gross = sumAmounts(lines.map((l) => l.amount));
  const route = cs.data?.route?.warnings?.filter((w) => w.needs_ack) ?? [];
  const setLine = (i: number, k: keyof BillLine, v: string) => { setLines(lines.map((l, j) => (j === i ? { ...l, [k]: v } : l))); setDirty(true); };
  const lineErr = (l: BillLine) => !isAmount(l.amount) || !isAmount(l.unit_price) || !/^\d+(\.\d{1,3})?$/.test(l.qty);

  async function save() {
    const ops: PatchOp[] = lines.map((l, i) => ({ op: "replace", path: `/bill_lines/${i}`, value: l }));
    ops.push({ op: "replace", path: "/totals", value: { gross, discounts: c.payload.totals.discounts, claimed: minus(gross, c.payload.totals.discounts) } });
    try { await api("PUT", `/v1/cases/${id}/claim`, ops, { ifMatch: c.etag }); setDirty(false); notify("Saved as a new version"); await q.refetch(); }
    catch (e) { if (e instanceof ApiError && [409, 412].includes(e.problem.status)) setConflict(true); else notify(message(e), "bad"); }
  }
  const unacked = [...warnings.map((w) => w.code), ...route.map((r) => r.code)].filter((x, i, a) => a.indexOf(x) === i);
  const canSign = is("officer") && status === "ready_for_review" && !c.has_errors && !dirty && unacked.every((x) => ack.includes(x)) && !c.signoff;
  return (
    <div className="space-y-4">
      <Card title={`Claim draft v${c.version}`} actions={<>
        {editable && <Button variant="secondary" disabled={!dirty || lines.some(lineErr)} onClick={save}>Save changes</Button>}
        {is("officer") && status === "ready_for_review" && !c.signoff && <Button variant="secondary" onClick={() => setConfirm("return")}>Return to builder</Button>}
        {canSign && <Button onClick={() => setConfirm("signoff")}>Sign off</Button>}
        {is("officer") && c.signoff && status === "ready_for_review" && <Button onClick={() => setConfirm("submit")} disabled={c.ready_to_submit ? !c.ready_to_submit.ok : false}>Submit to insurer</Button>}</>}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={c.has_errors ? "bad" : "good"} icon={c.has_errors ? "✕" : "✓"}>{c.has_errors ? `${errors.length} errors` : "No blocking errors"}</Badge>
          <Badge tone="neutral">source: {c.source}</Badge>{c.signoff && <Badge tone="good" icon="✓">Signed off</Badge>}{dirty && <Badge tone="warn" icon="!">Unsaved changes</Badge>}
        </div>
        {c.ready_to_submit && !c.ready_to_submit.ok && c.signoff && <p className="mt-2 text-sm text-amber-900">Not ready to submit: {c.ready_to_submit.reasons.join(", ")}</p>}
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card title="Patient and admission">
            <dl className="grid grid-cols-2 gap-2 text-sm md:grid-cols-3">
              {[["Patient", c.payload.patient.full_name], ["Policy", c.payload.patient.policy_number], ["Member", c.payload.patient.member_id], ["Admitted", c.payload.admission.admitted_on], ["Discharged", c.payload.admission.discharged_on], ["Doctor", c.payload.admission.treating_doctor || "—"], ["Diagnosis", c.payload.admission.diagnosis_codes.join(", ") || "—"], ["Procedures", c.payload.admission.procedure_codes.join(", ") || "—"]].map(([k, v]) => <div key={k}><dt className="text-xs text-slate-600">{k}</dt><dd>{v}</dd></div>)}
            </dl>
          </Card>
          <Card title="Bill lines">
            <div className="overflow-x-auto"><table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-slate-700"><tr>{["#", "Description", "Category", "Qty", "Unit price", "Amount", "Source"].map((h) => <th key={h} scope="col" className="px-1 py-1">{h}</th>)}</tr></thead>
              <tbody>{lines.map((l, i) => (
                <tr key={i} className={`border-t ${lineErr(l) ? "bg-red-50" : ""}`}>
                  <td className="px-1">{l.line_no}</td>
                  <td className="px-1">{editable ? <input aria-label={`Description line ${l.line_no}`} className={inputCls + " min-w-[15rem]"} value={l.description} onChange={(e) => setLine(i, "description", e.target.value)} /> : l.description}</td>
                  <td className="px-1">{editable ? <select aria-label={`Category line ${l.line_no}`} className={inputCls + " min-w-[8.5rem]"} value={l.category} onChange={(e) => setLine(i, "category", e.target.value)}>{CATEGORIES.map((x) => <option key={x}>{x}</option>)}</select> : l.category}</td>
                  {(["qty", "unit_price", "amount"] as const).map((k) => <td key={k} className="px-1">{editable ? <input aria-label={`${k} line ${l.line_no}`} inputMode="decimal" className={inputCls + " w-24 text-right"} value={l[k]} onChange={(e) => setLine(i, k, e.target.value)} /> : <span className="block text-right">{k === "qty" ? l.qty : inr(l[k])}</span>}</td>)}
                  <td className="px-1 text-xs">{(() => {
                    const src = docs.data?.documents.find((d) => d.id === l.source_doc_id);
                    return src ? <button className="text-blue-800 underline" onClick={() => setView({ doc: { id: src.id, filename: src.filename, pages: src.pages, doc_type: src.doc_type }, page: l.source_page ?? 1 })} aria-label={`Show line ${l.line_no} in ${src.filename}`}>{src.filename}{l.source_page ? ` p.${l.source_page}` : ""}</button> : "—";
                  })()}</td>
                </tr>))}</tbody>
              <tfoot><tr className="border-t font-medium"><td colSpan={5} className="px-1 py-2 text-right">Gross</td><td className="px-1 text-right">{inr(gross)}</td><td /></tr>
                <tr><td colSpan={5} className="px-1 text-right">Discounts</td><td className="px-1 text-right">{inr(c.payload.totals.discounts)}</td><td /></tr>
                <tr className="font-semibold"><td colSpan={5} className="px-1 text-right">Claimed</td><td className="px-1 text-right">{inr(minus(gross, c.payload.totals.discounts))}</td><td /></tr></tfoot>
            </table></div>
            {gross !== c.payload.totals.gross && !dirty && <p className="mt-2 text-sm text-red-800">Stored gross {inr(c.payload.totals.gross)} differs from the sum of lines.</p>}
          </Card>
        </div>
        <div className="space-y-4">
          <Card title="Checks">
            {errors.length === 0 && warnings.length === 0 ? <p className="text-sm text-slate-600">Nothing to fix.</p> : (
              <ul className="space-y-2 text-sm">
                {errors.map((e, i) => <li key={i} className="rounded bg-red-50 p-2 text-red-900"><b>✕ {e.code}</b> {e.message}{e.field && <span className="block text-xs">{e.field}</span>}</li>)}
                {warnings.map((w, i) => <li key={i} className="rounded bg-amber-50 p-2 text-amber-900"><label className="flex gap-2"><input type="checkbox" checked={ack.includes(w.code)} onChange={(e) => setAck(e.target.checked ? [...ack, w.code] : ack.filter((x) => x !== w.code))} /><span><b>! {w.code}</b> {w.message}<span className="block text-xs">I have seen this</span></span></label></li>)}
              </ul>)}
            {route.map((r) => <p key={r.code} className="mt-2 rounded bg-amber-50 p-2 text-sm text-amber-900"><label className="flex gap-2"><input type="checkbox" checked={ack.includes(r.code)} onChange={(e) => setAck(e.target.checked ? [...ack, r.code] : ack.filter((x) => x !== r.code))} />{r.message}</label></p>)}
          </Card>
          <Card title="Documents in this claim"><ul className="space-y-1 text-sm">{c.payload.documents.map((d) => <li key={d}>{docs.data?.documents.find((x) => x.id === d)?.filename ?? d}</li>)}</ul></Card>
        </div>
      </div>

      <ConfirmDialog open={confirm === "signoff"} title="Sign off this claim?" confirmLabel="Sign off" busy={act.isPending} onClose={() => setConfirm(null)}
        body={<><p>You confirm the claim of <b>{inr(c.payload.totals.claimed)}</b> is accurate and supported by the documents.</p><textarea aria-label="Comment" className={inputCls + " mt-2"} placeholder="Comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} /></>}
        onConfirm={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/claim/signoff`, body: { decision: "approved", comment: comment || null, acknowledged_warnings: ack } }, { onSuccess: () => setConfirm(null) })} />
      <ConfirmDialog open={confirm === "return"} title="Return to the builder?" confirmLabel="Return" busy={act.isPending} onClose={() => setConfirm(null)}
        body={<p>The draft is sent back for a fresh build. Your unsaved edits are not kept.</p>}
        onConfirm={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/claim/signoff`, body: { decision: "returned", comment: comment || "returned for rebuild", acknowledged_warnings: [] } }, { onSuccess: () => { setConfirm(null); setDirty(false); } })} />
      <ConfirmDialog open={confirm === "submit"} title="Submit to the insurer?" confirmLabel="Submit" busy={act.isPending} onClose={() => setConfirm(null)}
        body={<><ul className="mb-2 list-disc pl-5"><li>Claimed amount <b>{inr(c.payload.totals.claimed)}</b></li><li>{c.payload.documents.length} documents attached</li>{cs.data?.filing_deadline && <li>Filing deadline {cs.data.filing_deadline}</li>}</ul>
          {cs.data?.claim_type === "reimbursement" && cs.data.filing_deadline && new Date(cs.data.filing_deadline) < new Date() && <label className="block">Reason for late filing<textarea className={inputCls} value={late} onChange={(e) => setLate(e.target.value)} /></label>}</>}
        onConfirm={() => act.mutate({ method: "POST", path: `/v1/cases/${id}/claim/submit`, body: late ? { late_filing_reason: late } : {} }, { onSuccess: () => { setConfirm(null); notify("Submitted"); } })} />
      <Dialog open={conflict} title="Someone changed this claim" onClose={() => setConflict(false)}>
        <p className="mb-3 text-sm">A newer version exists. Reload to see it; your edits on this screen will be discarded.</p>
        <div className="flex justify-end"><Button onClick={() => { setConflict(false); setDirty(false); void q.refetch(); }}>Reload latest</Button></div>
      </Dialog>
      <DocViewer doc={view?.doc ?? null} page={view?.page ?? 1} onClose={() => setView(null)} />
      <Toast text={toast} tone={tone} />
    </div>
  );
}
