import { describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { invalidateFor } from "./sse";

describe("event invalidation", () => {
  it("invalidates the case, lists and dashboard", () => {
    const qc = new QueryClient();
    const hit: unknown[][] = [];
    qc.invalidateQueries = ((f: { queryKey: unknown[] }) => { hit.push(f.queryKey); return Promise.resolve(); }) as never;
    invalidateFor(qc, { type: "case.status_changed", case_id: "c1" });
    expect(hit).toEqual([["case", "c1"], ["cases"], ["dashboard"]]);
    hit.length = 0;
    invalidateFor(qc, { type: "query.new", case_id: "c1" });
    expect(hit).toEqual([["case", "c1"], ["queries"], ["dashboard"]]);
  });
});
