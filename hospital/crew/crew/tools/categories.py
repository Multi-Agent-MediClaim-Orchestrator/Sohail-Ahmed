from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache
def _rules() -> list[tuple[re.Pattern[str], str]]:
    raw = yaml.safe_load((Path(__file__).parent / "category_map.yaml").read_text())
    return [(re.compile(r["pattern"], re.I), r["category"]) for r in raw]


def lookup(description: str) -> str | None:
    """First matching rule wins; None when nothing matches (the only case where the LLM is consulted)."""
    for rx, cat in _rules():
        if rx.search(description):
            return cat
    return None
