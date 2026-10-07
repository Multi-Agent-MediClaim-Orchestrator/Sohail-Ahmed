import { describe, expect, it } from "vitest";
import { docChip } from "./docStatus";
import { inr, isAmount, minus, sumAmounts } from "./money";
import { ApiError, message } from "./errors";

const base = { scan_status: "clean", parse_status: "parsed", doc_type: "final_bill", lifecycle: "active" };

describe("docChip", () => {
  it("maps server fields to one display state", () => {
    expect(docChip({ ...base, scan_status: "pending" })).toBe("scanning");
    expect(docChip({ ...base, scan_status: "infected" })).toBe("blocked");
    expect(docChip({ ...base, lifecycle: "quarantined" })).toBe("blocked");
    expect(docChip({ ...base, parse_status: "failed" })).toBe("failed");
    expect(docChip({ ...base, parse_status: "needs_review" })).toBe("needs_review");
    expect(docChip({ ...base, parse_status: "processing" })).toBe("parsing");
    expect(docChip({ ...base, parse_status: "pending" })).toBe("quality");
    expect(docChip(base)).toBe("classified");
    expect(docChip({ ...base, doc_type: null })).toBe("needs_review");
  });
});

describe("money", () => {
  it("groups the Indian way", () => {
    expect(inr("125000.5")).toBe("₹1,25,000.50");
    expect(inr("999")).toBe("₹999.00");
    expect(inr("10000000")).toBe("₹1,00,00,000.00");
    expect(inr(null)).toBe("—");
  });
  it("never uses floating point", () => {
    expect(sumAmounts(["0.1", "0.2"])).toBe("0.30");
    expect(minus("100.10", "0.30")).toBe("99.80");
    expect(minus("10", "50")).toBe("0.00");
  });
  it("validates amounts", () => {
    expect(isAmount("12.50")).toBe(true);
    expect(isAmount("12.505")).toBe(false);
    expect(isAmount("-1")).toBe(false);
    expect(isAmount("")).toBe(false);
  });
});

describe("error messages", () => {
  it("prefers friendly text for known codes and falls back to the server detail", () => {
    expect(message(new ApiError({ status: 409, code: "version_conflict" }))).toMatch(/Reload/);
    expect(message(new ApiError({ status: 422, code: "weird", detail: "Specific detail" }))).toBe("Specific detail");
    expect(message(new Error("boom"))).toBe("boom");
  });
});
