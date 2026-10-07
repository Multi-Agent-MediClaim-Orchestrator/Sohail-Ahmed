import { NextRequest, NextResponse } from "next/server";

export function middleware(req: NextRequest) {
  const p = req.nextUrl.pathname;
  if (p === "/login" || p.startsWith("/api/auth")) return NextResponse.next();
  if (!req.cookies.get("hs")) {
    if (p.startsWith("/api/")) return NextResponse.json({ code: "unauthenticated", status: 401 }, { status: 401 });
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", p);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}
export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
