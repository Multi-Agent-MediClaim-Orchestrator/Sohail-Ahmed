"""Name normalisation and similarity (01-insurer-db §8.1, 03-03 §6.3). Same function is used at seed and match time."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "kumari", "master", "baby", "prof", "late", "mohd", "md"}


def normalise_name(name: str) -> str:
    """NFKD, strip accents, lower, drop punctuation and honorifics, collapse spaces."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = [t for t in s.split() if t not in HONORIFICS]
    return " ".join(tokens)


def _initials_match(a: list[str], b: list[str]) -> bool:
    """'ravi kumar s' vs 'ravi kumar sharma': a lone initial matches a token starting with it."""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    used: set[int] = set()
    for t in short:
        ok = False
        for i, u in enumerate(long_):
            if i in used:
                continue
            if t == u or (len(t) == 1 and u.startswith(t)) or (len(u) == 1 and t.startswith(u)):
                used.add(i)
                ok = True
                break
        if not ok:
            return False
    return True


def name_similarity(a: str, b: str) -> float:
    """max(Jaro-Winkler, token-set ratio); initials expansion counts as a near match (0.95)."""
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    jw = JaroWinkler.similarity(na, nb)
    ts = fuzz.token_set_ratio(na, nb) / 100
    best = max(jw, ts)
    if _initials_match(na.split(), nb.split()):
        best = max(best, 0.95)
    return round(best, 3)
