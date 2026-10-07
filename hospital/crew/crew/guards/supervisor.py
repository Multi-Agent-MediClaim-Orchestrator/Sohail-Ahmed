"""Deterministic supervisor checklist (doc 09 task 8). It can only pass or flag; it never rewrites.
An LLM review can be layered on top, but these rules are the gate."""

from __future__ import annotations

import re
from typing import Any

RULES = [
    (
        "payment_promise",
        re.compile(r"\b(payment will be|will be (?:paid|released|approved)|we guarantee)\b", re.I),
    ),
    ("liability_admission", re.compile(r"\b(our fault|we admit|negligen)", re.I)),
    ("medical_advice", re.compile(r"\byou should (?:take|stop|start)\b", re.I)),
    ("link_or_contact", re.compile(r"https?://|www\.|[\w.]+@[\w-]+\.\w+|(?<!\d)[6-9]\d{9}(?!\d)")),
    (
        "instruction_like",
        re.compile(r"\b(ignore (?:all |the )?(?:previous|above)|disregard|system prompt)\b", re.I),
    ),
]


def verdict(texts: list[str]) -> dict[str, Any]:
    issues = []
    for code, rx in RULES:
        for t in texts:
            if m := rx.search(t):
                issues.append(
                    {"severity": "blocker", "code": code, "detail": f"matched '{m.group(0)}'"}
                )
                break
    score = max(0.0, 1.0 - 0.3 * len(issues))
    return {"pass": not issues, "score": round(score, 2), "issues": issues}
