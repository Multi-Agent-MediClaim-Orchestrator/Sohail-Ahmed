"use client";
import { Button } from "@/components/ui";

/** A component that throws must not blank the whole app: show what happened and let the user retry. */
export default function SectionError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div role="alert" className="mx-auto mt-10 max-w-lg rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900">
      <p className="font-semibold">This page hit a problem.</p>
      <p className="mt-1">Nothing was lost. Try again, or go back to the case list.</p>
      <p className="mt-1 text-xs text-red-800">{error.message}{error.digest ? ` (ref ${error.digest})` : ""}</p>
      <div className="mt-3"><Button onClick={reset}>Try again</Button></div>
    </div>
  );
}
