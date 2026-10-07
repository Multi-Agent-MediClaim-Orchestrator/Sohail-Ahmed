import { NextRequest, NextResponse } from "next/server";
import { COOKIE, accessToken } from "@/lib/server/session";

const API = process.env.HOSP_API_URL ?? "http://localhost:8100";
const PASS = ["content-type", "idempotency-key", "if-match", "accept"];
export const dynamic = "force-dynamic";

async function proxy(req: NextRequest, ctx: { params: { path: string[] } }) {
  const tok = await accessToken(req.cookies.get(COOKIE)?.value);
  if (!tok) return NextResponse.json({ code: "unauthenticated", title: "Sign in required", status: 401 }, { status: 401 });
  const url = `${API}/${ctx.params.path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${tok}` });
  for (const h of PASS) { const v = req.headers.get(h); if (v) headers.set(h, v); }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  const up = await fetch(url, {
    method: req.method, headers, redirect: "manual", cache: "no-store",
    body: hasBody ? req.body : undefined,
    // @ts-expect-error duplex is required for streamed request bodies
    duplex: hasBody ? "half" : undefined,
  });
  const out = new Headers();
  for (const h of ["content-type", "etag", "location", "idempotent-replay", "retry-after"]) { const v = up.headers.get(h); if (v) out.set(h, v); }
  if (up.status >= 300 && up.status < 400 && up.headers.get("location")) {  // document downloads redirect to a presigned URL
    return NextResponse.json({ redirect: up.headers.get("location") }, { status: 200 });
  }
  return new NextResponse(up.status === 204 ? null : up.body, { status: up.status, headers: out });
}
export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
