import { http, HttpResponse } from "msw";
import { approvals, cases, CASE_ID, configVersions, escalations, queriesFull, settlements, workspace } from "./data";

export const calls: { method: string; path: string; headers: Record<string, string>; body?: unknown }[] = [];

async function record(request: Request) {
  let body: unknown;
  try {
    body = await request.clone().json();
  } catch {
    body = undefined;
  }
  calls.push({ method: request.method, path: new URL(request.url).pathname, headers: Object.fromEntries(request.headers.entries()), body });
}

export const handlers = [
  http.get("*/api/v1/cases/:id/queries", () => HttpResponse.json({ items: queriesFull })),
  http.patch("*/api/v1/queries/:id", async ({ request }) => { await record(request); return HttpResponse.json({ ok: true }); }),
  http.post("*/api/v1/queries/:id/send", async ({ request }) => { await record(request); return HttpResponse.json({ status: "open" }); }),
  http.post("*/api/v1/cases/:id/queries/draft", async ({ request }) => { await record(request); return HttpResponse.json({ accepted: true }, { status: 202 }); }),
  http.get("*/api/v1/admin/config/domains", () => HttpResponse.json({ domains: ["thresholds", "policy_rules"] })),
  http.get("*/api/v1/admin/config/:domain/:name/versions", () => HttpResponse.json({ items: configVersions })),
  http.get("*/api/v1/admin/config/:domain", () => HttpResponse.json({ names: ["default"] })),
  http.post("*/api/v1/admin/config/:domain/:name/versions/:v", async ({ request }) => { await record(request); return HttpResponse.json({ ok: true }); }),
  http.get("*/api/v1/cases", ({ request }) => {
    const status = new URL(request.url).searchParams.getAll("status");
    return HttpResponse.json({ items: status.length ? cases.filter((c) => status.includes(c.status)) : cases, next_cursor: null });
  }),
  http.get("*/api/v1/cases/:id/workspace", ({ params }) => {
    if (params.id !== CASE_ID) return HttpResponse.json({ code: "unknown_claim", title: "Unknown claim", status: 404 }, { status: 404 });
    return HttpResponse.json(workspace, { headers: { ETag: workspace.case.etag } });
  }),
  http.post("*/api/v1/cases/:id/decision/submit", async ({ request }) => {
    await record(request);
    if (request.headers.get("If-Match") !== workspace.case.etag) return HttpResponse.json({ code: "stale_etag", title: "Precondition failed", status: 412 }, { status: 412 });
    return HttpResponse.json({ decision_id: "dec-1", status: "awaiting_approval" });
  }),
  http.post("*/api/v1/decisions/:id/approvals", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ status: "recorded" });
  }),
  http.get("*/api/v1/approvals/queue", () => HttpResponse.json({ items: approvals })),
  http.get("*/api/v1/escalations", () => HttpResponse.json({ items: escalations })),
  http.get("*/api/v1/settlements", () => HttpResponse.json({ items: settlements, limit: 50, offset: 0 })),
  http.post("*/api/v1/settlements/:id/retry", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ status: "queued" });
  }),
  http.post("*/api/v1/cases/:id/verification/rerun", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ accepted: true }, { status: 202 });
  }),
  http.post("*/api/v1/cases/:id/findings/:code/override", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ ok: true });
  }),
  http.post("*/api/v1/cases/:id/pii-reveal", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ full_name: "Ravi Kumar" });
  }),
];
