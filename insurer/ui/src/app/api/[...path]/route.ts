import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

// BFF proxy: the browser only ever talks to this route; the token stays in an httpOnly cookie and only insurer-api is reachable.
const API = process.env.INS_API_URL ?? "http://localhost:8100";
const FORWARD = ["content-type", "idempotency-key", "if-match", "accept", "last-event-id"];

async function handler(req: NextRequest, { params }: { params: { path: string[] } }) {
  const token = cookies().get("ins_token")?.value;
  if (!token) return NextResponse.json({ code: "unauthenticated", title: "Sign in required", status: 401 }, { status: 401 });
  const target = `${API}/${params.path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${token}` });
  FORWARD.forEach((h) => req.headers.get(h) && headers.set(h, req.headers.get(h)!));
  const upstream = await fetch(target, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : await req.text(), cache: "no-store" });
  const out = new Headers();
  ["content-type", "etag", "cache-control"].forEach((h) => upstream.headers.get(h) && out.set(h, upstream.headers.get(h)!));
  return new NextResponse(upstream.body, { status: upstream.status, headers: out });
}

export { handler as GET, handler as POST, handler as PATCH, handler as PUT, handler as DELETE };
