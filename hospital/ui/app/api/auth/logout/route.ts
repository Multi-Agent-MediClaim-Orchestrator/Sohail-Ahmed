import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { COOKIE, logout } from "@/lib/server/session";

export async function POST() {
  logout(cookies().get(COOKIE)?.value);
  const res = NextResponse.json({ ok: true });
  res.cookies.delete(COOKIE);
  return res;
}
