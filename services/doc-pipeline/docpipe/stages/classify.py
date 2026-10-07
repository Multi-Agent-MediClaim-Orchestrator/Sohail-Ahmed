"""S3: rule-based classification (deterministic first). The LLM is a fallback only when the rules are not decisive."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache
def rules() -> dict[str, dict[str, float]]:
    return yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "classify_rules.yaml").read_text()
    )


def scores(text: str, hint: str | None = None) -> dict[str, float]:
    """Raw (uncapped) scores. A strong keyword in the page header (the document's title) counts extra: a surgical
    final bill mentions OT charges and surgeon fees but is titled FINAL BILL."""
    low = text.lower()
    head = "\n".join([ln for ln in low.splitlines() if ln.strip()][:8])
    out: dict[str, float] = {}
    for t, kws in rules().items():
        s = sum(w for k, w in kws.items() if k in low)
        s += sum(0.5 for k, w in kws.items() if w >= 0.5 and k in head)
        out[t] = s + (0.15 if hint == t and s > 0 else 0.0)
    return out


def classify(
    text: str, hint: str | None = None, min_conf: float = 0.75, margin: float = 0.2
) -> tuple[str | None, float, bool]:
    """(doc_type or None when undecided, confidence in 0..1, decisive)."""
    sc = sorted(scores(text, hint).items(), key=lambda kv: -kv[1])
    top, second = sc[0], sc[1]
    conf = round(min(1.0, top[1]), 3)
    if top[1] >= min_conf and top[1] - second[1] >= margin:
        return top[0], conf, True
    return (top[0] if top[1] > 0 else None), conf, False
