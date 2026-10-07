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
    low = text.lower()
    out: dict[str, float] = {}
    for t, kws in rules().items():
        s = sum(w for k, w in kws.items() if k in low)
        out[t] = min(1.0, s + (0.15 if hint == t and s > 0 else 0.0))
    return out


def classify(
    text: str, hint: str | None = None, min_conf: float = 0.75, margin: float = 0.2
) -> tuple[str | None, float, bool]:
    """(doc_type or None when undecided, confidence, decisive)."""
    sc = sorted(scores(text, hint).items(), key=lambda kv: -kv[1])
    top, second = sc[0], sc[1]
    if top[1] >= min_conf and top[1] - second[1] >= margin:
        return top[0], round(top[1], 3), True
    return (top[0] if top[1] > 0 else None), round(top[1], 3), False
