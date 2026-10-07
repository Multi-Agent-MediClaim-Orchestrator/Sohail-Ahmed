import { NextResponse } from "next/server";
import { COOKIE, login } from "@/lib/server/session";

export async function POST(req: Request) {
  const { username, password } = await req.json().catch(() => ({}));
  if (typeof username !== "string" || typeof password !== "string") return NextResponse.json({ code: "bad_request" }, { status: 400 });
  const id = await login(username, password);
  if (!id) return NextResponse.json({ code: "invalid_credentials", detail: "Wrong user name or password." }, { status: 401 });
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, id, { httpOnly: true, sameSite: "lax", path: "/", secure: false, maxAge: 60 * 60 * 12 });
  return res;
}
