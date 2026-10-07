"""Grounding guard for drafted replies. Mirrors the API's G01-G06 so a draft that passes here passes there;
the API still re-verifies independently (doc 07)."""

from __future__ import annotations

import re
from typing import Any

from crew.tools.numbers import norm_text

FORBIDDEN = re.compile(
    r"\b(guarantee[sd]?|will be (?:approved|paid)|payment will be|legal action|sue|lawsuit|we admit|our fault|promise[sd]?)\b",
    re.I,
)
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
MAX_WORDS = 250


def _n(tok: str) -> str:
    t = tok.replace(",", "")
    return t.rstrip("0").rstrip(".") if "." in t else t


def check(draft: str, citations: list[dict[str, Any]], evidence: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if len(draft.strip()) < 20:
        out.append({"rule": "G01", "detail": "draft is too short"})
    for m in FORBIDDEN.finditer(draft):
        out.append({"rule": "G02", "detail": f"forbidden commitment: {m.group(0)}"})
    ev_nums = {_n(x) for x in NUM.findall(evidence)}
    for n in NUM.findall(draft):
        if len(n.replace(",", "")) >= 2 and _n(n) not in ev_nums:
            out.append({"rule": "G03", "detail": f"number not found in the record: {n}"})
    low = norm_text(evidence)
    for c in citations:
        q = (c.get("quote") or "").strip()
        if not q:
            out.append({"rule": "G04", "detail": f"citation {c.get('source_id')} has no quote"})
        elif norm_text(q) not in low:
            out.append({"rule": "G05", "detail": f"quote not in the record: {q[:40]}"})
    if not citations and NUM.search(draft):
        out.append({"rule": "G06", "detail": "numbers cited without any source"})
    if len(draft.split()) > MAX_WORDS:
        out.append({"rule": "G07", "detail": f"more than {MAX_WORDS} words"})
    if re.search(r"https?://|www\.|[\w.]+@[\w.]+", draft):
        out.append({"rule": "G08", "detail": "link or e-mail address in the draft"})
    return out
