import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ApprovalList } from "@/components/ApprovalList";
import { DecisionForm } from "@/components/DecisionForm";
import { WorkspaceView } from "@/components/WorkspaceView";
import { api, ApiError, can, formatMoney, parseRolesFromToken } from "@/lib/api";
import { calls } from "@/mocks/handlers";
import { workspace } from "@/mocks/data";
import { server } from "./setup";

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("lib", () => {
  it("formats rupees in lakh grouping", () => {
    expect(formatMoney("620000.00")).toContain("6,20,000.00");
    expect(formatMoney(null)).toBe("—");
  });
  it("sends Idempotency-Key and If-Match on mutations only", async () => {
    server.use(http.post("*/api/x", ({ request }) => HttpResponse.json({ k: request.headers.get("idempotency-key"), m: request.headers.get("if-match") })), http.get("*/api/x", ({ request }) => HttpResponse.json({ k: request.headers.get("idempotency-key") })));
    const post = await api<{ k: string; m: string }>("/x", { method: "POST", body: {}, etag: "e1" });
    expect(post.k).toMatch(/.{8,}/);
    expect(post.m).toBe("e1");
    expect((await api<{ k: string | null }>("/x")).k).toBeNull();
  });
  it("maps problem+json errors", async () => {
    server.use(http.get("*/api/y", () => HttpResponse.json({ code: "stale_etag", title: "Precondition failed" }, { status: 412 })));
    await expect(api("/y")).rejects.toMatchObject({ status: 412, code: "stale_etag" });
    await expect(api("/y")).rejects.toBeInstanceOf(ApiError);
  });
  it("reads roles from a token and gates by role (hint only)", () => {
    const tok = `h.${btoa(JSON.stringify({ roles: ["reviewer"] }))}.s`;
    expect(parseRolesFromToken(tok)).toEqual(["reviewer"]);
    expect(parseRolesFromToken("garbage")).toEqual([]);
    expect(can(["reviewer"], "approver")).toBe(false);
  });
});

describe("workspace", () => {
  it("renders header, masked member, documents, steps and calc breakdown", async () => {
    wrap(<WorkspaceView ws={workspace} roles={["reviewer"]} />);
    expect(screen.getByRole("heading", { name: "IC-2026-000101" })).toBeInTheDocument();
    expect(screen.getByText(/R\*\*\* K\*\*\*\*/)).toBeInTheDocument();
    expect(screen.queryByText(/Ravi Kumar/)).toBeNull();
    expect(screen.getByText(/discharge summary/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Calculation breakdown" });
    expect(table).toHaveTextContent("R-ROOM-CAP");
    expect(table).toHaveTextContent("1,90,000.00");
  });
  it("opens the first non-passed step, labels AI text, and shows the degraded banner", async () => {
    wrap(<WorkspaceView ws={workspace} roles={["reviewer"]} />);
    expect(screen.getByText("STAMP_MISSING")).toBeInTheDocument();
    expect(screen.getByText(/Automated explanation unavailable/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Identity/ }));
    expect(screen.getByText("AI explanation — not used for gating")).toBeInTheDocument();
  });
  it("shows the server's gate text and never computes a tier itself", () => {
    wrap(<WorkspaceView ws={{ ...workspace, recommendation: { ...workspace.recommendation!, gate_tier: "dual_approver" } }} roles={["reviewer"]} />);
    expect(screen.getByRole("status")).toHaveTextContent("Requires two approvers including one senior.");
  });
});

describe("decision form", () => {
  it("hides the form without a reviewer role and when the case is not ready", () => {
    render(<DecisionForm ws={workspace} roles={["approver"]} />);
    expect(screen.getByText(/do not have permission/)).toBeInTheDocument();
    render(<DecisionForm ws={{ ...workspace, case: { ...workspace.case, status: "verifying" } }} roles={["reviewer"]} />);
    expect(screen.getByText(/ready for decision/)).toBeInTheDocument();
  });
  it("submits the recommendation with If-Match and an Idempotency-Key", async () => {
    render(<DecisionForm ws={workspace} roles={["reviewer"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Confirm decision" }));
    expect(await screen.findByText(/Submitted:/)).toBeInTheDocument();
    const c = calls.find((x) => x.path.endsWith("/decision/submit"))!;
    expect(c.headers["if-match"]).toBe("etag-1");
    expect(c.headers["idempotency-key"]).toBeTruthy();
    expect(c.body).toMatchObject({ outcome: "partial", approved_amount: "190000.00", override_reason: null });
  });
  it("requires a written reason before an override can be submitted", async () => {
    render(<DecisionForm ws={workspace} roles={["reviewer"]} />);
    await userEvent.selectOptions(screen.getByLabelText("Outcome"), "approve");
    const btn = screen.getByRole("button", { name: "Confirm decision" });
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Reason for changing/), "Hospital supplied the stamped bill offline");
    expect(btn).toBeEnabled();
  });
  it("tells the user when the case changed underneath them (412)", async () => {
    server.use(http.post("*/api/v1/cases/:id/decision/submit", () => HttpResponse.json({ code: "stale_etag" }, { status: 412 })));
    render(<DecisionForm ws={workspace} roles={["reviewer"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Confirm decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed while you were viewing/);
  });
});

describe("approvals", () => {
  it("lists tasks, requires a reason to reject, and posts the vote", async () => {
    wrap(<ApprovalList />);
    expect(await screen.findByText("IC-2026-000101")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByText(/reason is required/)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
    await userEvent.type(screen.getByLabelText(/Reason for IC-2026-000101/), "Documents do not support the stay");
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ verdict: "reject", comment: "Documents do not support the stay" });
  });
});
