import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AdminPage from "@/app/admin/page";
import { DocumentViewer } from "@/components/DocumentViewer";
import { QueriesPanel } from "@/components/QueriesPanel";
import { WorkspaceView } from "@/components/WorkspaceView";
import { calls } from "@/mocks/handlers";
import { workspace } from "@/mocks/data";
import { useEventStream } from "@/lib/useEventStream";

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });
const wrap = (ui: React.ReactElement, qc = client()) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);

describe("documents and evidence", () => {
  const f = [{ code: "STAMP_MISSING", severity: "warning" as const, evidence: [{ doc_id: "d2", page: 1, bbox: [0.6, 0.8, 0.9, 0.95] as [number, number, number, number] }] }];
  it("draws an overlay only for the selected document and page, and reports clicks", async () => {
    const click = vi.fn();
    const { rerender } = render(<DocumentViewer caseId="c1" docs={workspace.documents} findings={f} selected="d2" onSelect={() => {}} onFindingClick={click} />);
    await userEvent.click(screen.getByRole("button", { name: "Evidence STAMP_MISSING" }));
    expect(click).toHaveBeenCalledWith("STAMP_MISSING");
    rerender(<DocumentViewer caseId="c1" docs={workspace.documents} findings={f} selected="d1" onSelect={() => {}} />);
    expect(screen.queryByRole("button", { name: /Evidence/ })).toBeNull();
  });
  it("pages and zooms within bounds and loads the document through the BFF", async () => {
    render(<DocumentViewer caseId="c1" docs={workspace.documents} findings={[]} selected="d2" onSelect={() => {}} />);
    expect(screen.getByTitle("bill.pdf")).toHaveAttribute("src", "/api/v1/cases/c1/documents/d2/download#page=1");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    for (let i = 0; i < 12; i++) await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByText(/300%/)).toBeInTheDocument();
  });
});

describe("keyboard shortcuts and overrides", () => {
  it("j and k move through documents, ? toggles help, and keys are ignored while typing", async () => {
    wrap(<WorkspaceView ws={workspace} roles={["reviewer"]} />);
    expect(screen.getByRole("button", { name: /discharge summary/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.keyboard("j");
    expect(screen.getByRole("button", { name: /final bill/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.keyboard("k");
    expect(screen.getByRole("button", { name: /discharge summary/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.keyboard("?");
    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Outcome"));
    await userEvent.keyboard("j");
    expect(screen.getByRole("button", { name: /discharge summary/ })).toHaveAttribute("aria-pressed", "true");
  });
  it("r re-runs verification for reviewers only", async () => {
    const { unmount } = wrap(<WorkspaceView ws={workspace} roles={["approver"]} />);
    await userEvent.keyboard("r");
    expect(calls).toHaveLength(0);
    unmount();
    wrap(<WorkspaceView ws={workspace} roles={["reviewer"]} />);
    await userEvent.keyboard("r");
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/verification/rerun"))).toBe(true));
  });
  it("override needs a written reason and posts it", async () => {
    wrap(<WorkspaceView ws={workspace} roles={["reviewer"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Override" }));
    const btn = screen.getByRole("button", { name: "Confirm override" });
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Override reason/), "Stamp confirmed by phone");
    await userEvent.click(btn);
    await waitFor(() => expect(calls.find((c) => c.path.includes("/override"))?.body).toEqual({ reason: "Stamp confirmed by phone" }));
  });
});

describe("queries panel", () => {
  it("shows the AI draft, counts characters, saves edits and requires confirmation to send", async () => {
    wrap(<QueriesPanel caseId="c1" roles={["reviewer"]} />);
    const box = await screen.findByLabelText("Query text");
    expect(screen.getByText("AI draft")).toBeInTheDocument();
    await userEvent.type(box, " Thank you.");
    expect(screen.getByText(/\/1200/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save edits" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toMatchObject({ text: expect.stringContaining("Thank you.") }));
    await userEvent.click(screen.getByRole("button", { name: /Send to hospital/ }));
    expect(screen.getByRole("dialog", { name: "Confirm send" })).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/send"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Confirm send" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/send"))).toBe(true));
  });
  it("is read-only for approvers and shows the hospital reply with triage", async () => {
    wrap(<QueriesPanel caseId="c1" roles={["approver"]} />);
    await screen.findByText(/Earlier request/);
    expect(screen.queryByLabelText("Query text")).toBeNull();
    expect(screen.getByText(/Attached\./)).toBeInTheDocument();
    expect(screen.getByText(/Triage: sufficient/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Draft query/ })).toBeNull();
  });
});

describe("event stream and admin", () => {
  it("refreshes the workspace on events, reports live or paused, and closes on unmount", async () => {
    const qc = client();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const handlers: Record<string, () => void> = {};
    const es = { onopen: null as null | (() => void), onerror: null as null | (() => void), onmessage: null as null | (() => void), close: vi.fn(), addEventListener: (t: string, h: () => void) => { handlers[t] = h; } };
    const { result, unmount } = renderHook(() => useEventStream("c1", () => es as unknown as EventSource), { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
    expect(result.current).toBe("connecting");
    act(() => es.onopen!());
    expect(result.current).toBe("live");
    act(() => handlers["case.status_changed"]());
    expect(spy).toHaveBeenCalledWith({ queryKey: ["workspace", "c1"] });
    act(() => es.onerror!());
    expect(result.current).toBe("paused");
    unmount();
    expect(es.close).toHaveBeenCalled();
  });
  it("admin lists domains and versions and only offers actions on drafts", async () => {
    wrap(<AdminPage />);
    await userEvent.click(await screen.findByRole("button", { name: "thresholds" }));
    await userEvent.click(await screen.findByRole("button", { name: "default" }));
    const table = await screen.findByRole("table", { name: "Versions" });
    await waitFor(() => expect(table).toHaveTextContent("raise T_auto"));
    expect(screen.getAllByRole("button", { name: "Publish" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Dry-run" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith(":dry-run"))).toBe(true));
  });
});
