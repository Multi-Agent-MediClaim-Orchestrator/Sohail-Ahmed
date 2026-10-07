"""Orchestration (doc 03 §6): quality per page, stamps on the right page(s), registry match, conditional escalation."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from vision import quality, stamps
from vision.escalate import Escalator
from vision.settings import Settings

# doc_type -> (expect_all, expect_any groups). Bills must carry the hospital's stamp (user decision).
EXPECT: dict[str, tuple[list[str], list[list[str]]]] = {
    "pharmacy_bill": (["hospital_stamp"], []), "procedure_bill": (["hospital_stamp"], []),
    "final_bill": (["hospital_stamp"], []), "itemised_bill": (["hospital_stamp"], []),
    "prescription": ([], [["doctor_stamp", "signature"]]),
    "discharge_summary": ([], [["hospital_stamp", "doctor_stamp", "signature"]]),
    "lab_report": ([], [["hospital_stamp", "signature"]]), "radiology_report": ([], [["hospital_stamp", "signature"]]),
    "claim_form": ([], [["signature"]]),
}  # fmt: skip


def search_order(n_pages: int) -> list[int]:
    """Last page first, then the rest backwards: a stamp may sit on page n-1 of a multi-page bill."""
    return list(range(n_pages, 0, -1))


async def analyze(pages: list[np.ndarray], s: Settings, registry: list[dict[str, Any]], *, doc_type: str | None = None,
                  expect: list[str] | None = None, expect_any: list[list[str]] | None = None, esc: Escalator | None = None,
                  doc_id: str = "") -> dict[str, Any]:  # fmt: skip
    t0 = time.monotonic()
    if doc_type and expect is None and expect_any is None:
        expect, expect_any = (
            list(EXPECT.get(doc_type, ([], []))[0]),
            [list(g) for g in EXPECT.get(doc_type, ([], []))[1]],
        )
    expect, expect_any = expect or [], expect_any or []
    qs = [quality.measure(p, i + 1, s) for i, p in enumerate(pages)]
    wanted = sorted({k for k in expect} | {k for g in expect_any for k in g})
    found: list[stamps.Stamp] = []
    if wanted:
        for pg in search_order(len(pages)):
            if qs[pg - 1].blank_page:
                continue
            dets = stamps.detect(pages[pg - 1], pg)
            for d in dets:
                if d.kind in ("hospital_stamp", "seal", "doctor_stamp"):
                    d.ocr_text, d.ocr_conf = stamps.ocr_crop(pages[pg - 1], d.bbox)
                    d.matches_registry, d.registry_hospital, d.registry_score = (
                        stamps.match_registry(d.ocr_text, registry)
                    )
            found += dets
            if all(stamps.present(found, [k], s.det_conf_min)[k] for k in expect) and (
                not expect_any
                or any(all(stamps.present(found, g, s.det_conf_min).values()) for g in expect_any)
            ):
                break  # everything required has been found: stop looking
    pres = stamps.present(found, wanted, s.det_conf_min) if wanted else {}
    missing = [k for k in expect if not pres.get(k)]
    any_ok = (not expect_any) or any(any(pres.get(k) for k in g) for g in expect_any)
    missing += [] if any_ok else ["|".join(expect_any[0])] if expect_any else []
    all_legible = all(q.legible for q in qs)
    required = None if not wanted else (not missing)
    escalations: list[dict[str, Any]] = []
    needs_confirm = False
    reasons: list[str] = []
    if wanted and missing and all_legible and esc is not None:
        last = qs[-1].page
        r = await esc.check(pages[last - 1], wanted, doc_id)
        if r is None:
            reasons.append("escalation_skipped")
        else:
            escalations.append(r)
            needs_confirm = (
                True  # an LLM verdict is never final: a human confirms (absence or presence)
            )
    return {
        "pages": [q.dict() for q in qs], "stamps": [x.dict() for x in found], "required_stamp_present": required,
        "missing_kinds": missing, "all_pages_legible": all_legible, "escalated": bool(escalations), "escalations": escalations,
        "needs_human_confirm": needs_confirm, "reasons": reasons, "version": "1.0.0",
        "timings_ms": {"total": int((time.monotonic() - t0) * 1000)},
    }  # fmt: skip


def hospital_quality(report: dict[str, Any]) -> dict[str, Any]:
    """The QualityIn body hospital-api stores for a document."""
    qs = [quality.PageQuality(**p) for p in report["pages"]]
    flags = sorted({r for q in qs for r in q.reasons})
    if not report["all_pages_legible"]:
        flags = sorted({*flags, "unreadable"})
    stamp_seen = any(
        s["kind"] in ("hospital_stamp", "seal") and s["det_conf"] >= 0.5 for s in report["stamps"]
    )
    return {
        "quality_score": quality.score(qs),
        "flags": flags,
        "has_required_stamp": (
            True if stamp_seen else (False if report["all_pages_legible"] else None)
        ),
    }
