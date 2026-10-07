// Thin client for insurer-api (through the BFF proxy at /api/*). Display + request building only: no business rules here.

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export type Money = string; // always a decimal string from the server; never parsed for arithmetic in the UI

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 });
export function formatMoney(v: Money | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? inr.format(n) : String(v);
}

export function newIdempotencyKey(): string {
  return (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`) as string;
}

type Opts = { method?: string; body?: unknown; etag?: string; base?: string; idempotencyKey?: string };

export async function api<T>(path: string, opts: Opts = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["Idempotency-Key"] = opts.idempotencyKey ?? newIdempotencyKey();
  if (opts.etag) headers["If-Match"] = opts.etag;
  const res = await fetch(`${opts.base ?? "/api"}${path}`, { method, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body), credentials: "same-origin" });
  if (!res.ok) {
    let code = "error";
    let msg = res.statusText;
    try {
      const j = await res.json();
      code = j.code ?? code;
      msg = j.detail ?? j.title ?? msg;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, code, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function parseRolesFromToken(token: string | null): string[] {
  if (!token) return [];
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return Array.isArray(payload.roles) ? payload.roles : [];
  } catch {
    return [];
  }
}

export function can(roles: string[], ...allowed: string[]): boolean {
  return roles.some((r) => allowed.includes(r)); // UI hint only; the server enforces
}
