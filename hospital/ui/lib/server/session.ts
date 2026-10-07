import "server-only";
import { randomBytes } from "crypto";

/** Tokens never reach the browser: the cookie carries an opaque session id, tokens live in server memory.
 *  Localhost demo; a restart signs everyone out. */
interface Sess { access: string; refresh: string; exp: number; user: string }
const g = globalThis as unknown as { __sessions?: Map<string, Sess> };
const sessions = (g.__sessions ??= new Map<string, Sess>());

const TOKEN_URL = process.env.KEYCLOAK_TOKEN_URL ?? "http://localhost:8080/realms/hospital/protocol/openid-connect/token";
const CLIENT = process.env.HOSP_UI_CLIENT_ID ?? "hospital-dev";
export const COOKIE = "hs";

async function token(form: Record<string, string>): Promise<Sess | null> {
  const r = await fetch(TOKEN_URL, { method: "POST", body: new URLSearchParams({ client_id: CLIENT, ...form }), cache: "no-store" });
  if (!r.ok) return null;
  const j = await r.json();
  return { access: j.access_token, refresh: j.refresh_token, exp: Date.now() + (j.expires_in - 20) * 1000, user: form.username ?? "" };
}

export async function login(username: string, password: string): Promise<string | null> {
  const s = await token({ grant_type: "password", username, password });
  if (!s) return null;
  const id = randomBytes(24).toString("hex");
  sessions.set(id, s);
  return id;
}

export function logout(id: string | undefined) { if (id) sessions.delete(id); }

export async function accessToken(id: string | undefined): Promise<string | null> {
  const s = id ? sessions.get(id) : undefined;
  if (!s || !id) return null;
  if (s.exp > Date.now()) return s.access;
  const n = await token({ grant_type: "refresh_token", refresh_token: s.refresh });
  if (!n) { sessions.delete(id); return null; }
  sessions.set(id, { ...n, user: s.user });
  return n.access;
}
