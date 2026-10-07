import { NextRequest, NextResponse } from "next/server";
import { COOKIE, accessToken } from "@/lib/server/session";

const API = process.env.HOSP_API_URL ?? "http://localhost:8100";
export const dynamic = "force-dynamic";

/** EventSource cannot send headers: get a one-time ticket server-side, then pipe the upstream stream through. */
export async function GET(req: NextRequest) {
  const tok = await accessToken(req.cookies.get(COOKIE)?.value);
  if (!tok) return NextResponse.json({ code: "unauthenticated" }, { status: 401 });
  const scope = req.nextUrl.searchParams.get("scope") ?? "inbox";
  const caseId = req.nextUrl.searchParams.get("case_id");
  const t = await fetch(`${API}/v1/stream/ticket`, {
    method: "POST", cache: "no-store",
    headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ scope, case_id: caseId }),
  });
  if (!t.ok) return new NextResponse(await t.text(), { status: t.status });
  const { ticket } = await t.json();
  const h: Record<string, string> = {};
  const last = req.headers.get("last-event-id");
  if (last) h["Last-Event-ID"] = last;
  const up = await fetch(`${API}/v1/stream?ticket=${ticket}`, { headers: h, cache: "no-store", signal: req.signal });
  return new NextResponse(up.body, { status: up.status, headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no" } });
}
