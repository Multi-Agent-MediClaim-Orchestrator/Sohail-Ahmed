import { ApiError, ApiProblem } from "./errors";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export interface Opts { ifMatch?: string; idempotent?: boolean }

export async function api<T>(method: Method, path: string, body?: unknown, opts: Opts = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "DELETE" && opts.idempotent !== false) headers["Idempotency-Key"] = crypto.randomUUID();
  if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
  const r = await fetch(`/api/hosp${path}`, {
    method, headers, cache: "no-store",
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });
  if (r.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth")) window.location.href = "/login?next=" + encodeURIComponent(location.pathname);
  if (!r.ok) {
    const p = (await r.json().catch(() => ({}))) as Partial<ApiProblem>;
    throw new ApiError({ status: r.status, code: p.code ?? "error", title: p.title, detail: p.detail, errors: p.errors, trace_id: p.trace_id });
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}
export const get = <T,>(p: string) => api<T>("GET", p);

/** JSON Patch for the claim editor: only the paths the API allows. */
export interface PatchOp { op: "replace" | "add" | "remove"; path: string; value?: unknown }
