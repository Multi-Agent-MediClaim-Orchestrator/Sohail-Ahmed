"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { z } from "zod";
import { api } from "@/lib/api";
import { ApiError, message } from "@/lib/errors";
import { Badge, Button, Card, Field, inputCls } from "@/components/ui";

const schema = z.object({
  uhid: z.string().min(1, "Required"), full_name: z.string().min(2, "Enter the full name"),
  dob: z.string().refine((v) => v && new Date(v) <= new Date(), "Date of birth cannot be in the future"),
  gender: z.enum(["M", "F", "O"]), phone: z.string().regex(/^\+?\d{10,13}$/, "Enter a phone number with 10-13 digits"),
  insurer_name: z.string().min(1, "Required"), policy_number: z.string().min(1, "Required"), member_id: z.string().min(1, "Required"),
  admission_type: z.enum(["planned", "emergency"]), admitted_on: z.string().min(1, "Required"), discharged_on: z.string().optional(),
  treating_doctor: z.string().optional(), preauth_ref: z.string().optional(), claim_type: z.enum(["cashless", "reimbursement"]),
});
type F = z.infer<typeof schema>;
const EMPTY: F = { uhid: "", full_name: "", dob: "", gender: "M", phone: "", insurer_name: "", policy_number: "", member_id: "", admission_type: "planned", admitted_on: "", discharged_on: "", treating_doctor: "", preauth_ref: "", claim_type: "cashless" };
const KEY = "new-case-draft";

export default function NewCase() {
  const router = useRouter();
  const [f, setF] = useState<F>(EMPTY);
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [top, setTop] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // form draft survives a refresh in this tab only (sessionStorage, never localStorage: it holds patient data)
  useEffect(() => { try { const s = sessionStorage.getItem(KEY); if (s) setF({ ...EMPTY, ...JSON.parse(s) }); } catch { /* private mode */ } }, []);
  useEffect(() => { try { sessionStorage.setItem(KEY, JSON.stringify(f)); } catch { /* ignore */ } }, [f]);
  const set = (k: keyof F) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const input = (k: keyof F, label: string, type = "text", hint?: string) => (
    <Field label={label} error={errs[k]} hint={hint}><input className={inputCls} type={type} value={(f[k] as string) ?? ""} onChange={set(k)} aria-invalid={!!errs[k]} /></Field>
  );
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setTop(null);
    const r = schema.safeParse(f);
    if (!r.success) { setErrs(Object.fromEntries(r.error.issues.map((i) => [String(i.path[0]), i.message]))); return; }
    setErrs({}); setBusy(true);
    const v = r.data;
    try {
      const c = await api<{ id: string }>("POST", "/v1/cases", {
        patient: { uhid: v.uhid, full_name: v.full_name, dob: v.dob, gender: v.gender, phone: v.phone },
        policy: { insurer_name: v.insurer_name, policy_number: v.policy_number, member_id: v.member_id },
        claim_type: v.claim_type, admission_type: v.admission_type, admitted_on: v.admitted_on,
        ...(v.discharged_on ? { discharged_on: v.discharged_on } : {}), ...(v.treating_doctor ? { treating_doctor: v.treating_doctor } : {}),
        ...(v.preauth_ref ? { preauth_ref: v.preauth_ref } : {}),
      });
      try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
      router.push(`/cases/${c.id}/documents`);
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors?.length) setErrs(Object.fromEntries(err.problem.errors.map((x) => [(x.field ?? "").split(".").pop() ?? "", x.message ?? ""])));
      setTop(message(err));
    } finally { setBusy(false); }
  }
  const emergency = f.admission_type === "emergency";
  return (
    <form onSubmit={submit} className="mx-auto max-w-3xl space-y-4" noValidate>
      <h1 className="text-xl font-semibold">New case</h1>
      {top && <div role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">{top}</div>}
      <Card title="1. Patient"><div className="grid gap-3 md:grid-cols-2">
        {input("uhid", "Hospital ID (UHID)")}{input("full_name", "Full name")}{input("dob", "Date of birth", "date")}
        <Field label="Gender"><select className={inputCls} value={f.gender} onChange={set("gender")}><option value="M">Male</option><option value="F">Female</option><option value="O">Other</option></select></Field>
        {input("phone", "Phone", "tel", "Only the last 4 digits are shown after saving")}
      </div></Card>
      <Card title="2. Policy"><div className="grid gap-3 md:grid-cols-3">{input("insurer_name", "Insurer")}{input("policy_number", "Policy number")}{input("member_id", "Member ID")}</div></Card>
      <Card title="3. Admission"><div className="grid gap-3 md:grid-cols-2">
        <Field label="Admission type"><select className={inputCls} value={f.admission_type} onChange={set("admission_type")}><option value="planned">Planned</option><option value="emergency">Emergency</option></select></Field>
        {input("admitted_on", "Admitted on", "date")}{input("discharged_on", "Discharged on", "date")}{input("treating_doctor", "Treating doctor")}{input("preauth_ref", "Pre-auth reference", "text", "For cashless claims")}
      </div>{emergency && <p className="mt-3"><Badge tone="warn" icon="!">Emergency: tell the insurer within 24 hours of admission.</Badge></p>}</Card>
      <Card title="4. Claim type">
        <div className="flex gap-6 text-sm">{(["cashless", "reimbursement"] as const).map((t) => <label key={t} className="flex items-center gap-2"><input type="radio" name="ct" checked={f.claim_type === t} onChange={() => setF({ ...f, claim_type: t })} />{t === "cashless" ? "Cashless" : "Reimbursement"}</label>)}</div>
        <p className="mt-2 text-xs text-slate-600">The system confirms the route after saving and shows any warnings on the case.</p>
      </Card>
      <div className="flex justify-end gap-2"><Button type="button" variant="secondary" onClick={() => router.push("/cases")}>Cancel</Button><Button type="submit" disabled={busy}>Create case</Button></div>
    </form>
  );
}
