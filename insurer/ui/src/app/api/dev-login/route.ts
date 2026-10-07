import { NextResponse } from "next/server";

// Local development only: stores the pasted token in an httpOnly cookie and exposes the roles (non-secret hint) for rendering.
export async function POST(req: Request) {
  if (process.env.NODE_ENV === "production" && process.env.INS_ALLOW_DEV_LOGIN !== "1") return NextResponse.json({ code: "forbidden" }, { status: 403 });
  const { token } = (await req.json()) as { token?: string };
  if (!token) return NextResponse.json({ code: "bad_request" }, { status: 400 });
  let roles: string[] = [];
  try {
    roles = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString()).roles ?? [];
  } catch {
    return NextResponse.json({ code: "bad_token" }, { status: 400 });
  }
  const res = NextResponse.json({ ok: true, roles });
  res.cookies.set("ins_token", token, { httpOnly: true, sameSite: "strict", path: "/" });
  res.cookies.set("ins_roles", roles.join(","), { httpOnly: false, sameSite: "strict", path: "/" });
  return res;
}
