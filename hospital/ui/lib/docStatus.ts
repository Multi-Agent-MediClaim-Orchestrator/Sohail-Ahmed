import type { DocView } from "./types";

export type Chip = "scanning" | "quality" | "parsing" | "classified" | "needs_review" | "failed" | "blocked";

/** The one place that turns server fields into a display chip. Never infers business state. */
export function docChip(d: Pick<DocView, "scan_status" | "parse_status" | "doc_type" | "lifecycle">): Chip {
  if (d.lifecycle === "quarantined" || d.scan_status === "infected") return "blocked";
  if (d.scan_status === "pending") return "scanning";
  if (d.scan_status !== "clean") return "failed";
  switch (d.parse_status) {
    case "failed": return "failed";
    case "needs_review": return "needs_review";
    case "parsed": return d.doc_type ? "classified" : "needs_review";
    case "processing": return "parsing";
    default: return "quality";
  }
}
export const CHIP_TEXT: Record<Chip, string> = {
  scanning: "Scanning", quality: "Checking quality", parsing: "Reading", classified: "Ready",
  needs_review: "Needs review", failed: "Could not read", blocked: "Blocked (virus scan)",
};
export const CHIP_HELP: Partial<Record<Chip, string>> = {
  failed: "We could not read this file. Try re-parse or upload a clearer copy.",
  blocked: "File failed the virus scan and was removed.",
  needs_review: "Check the document type and the extracted values.",
};
