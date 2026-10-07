"""Tokenisation for sparse vectors, a deterministic hashing embedder for tests/offline use, and PII regexes."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

# alphanumeric tokens keep ICD codes ("I21.9"), clause numbers ("4.2.1") and procedure codes whole; no stemming
_WORD = re.compile(r"[A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)*")
_CODE = re.compile(r"\b(?:[A-Z]\d{2}(?:\.\d+)?|\d{2}\.\d{1,2}|[A-Z]{2,}\d{2,})\b")
STOP = frozenset("a an and are as at be by for from has have in is it of on or that the this to was were will with".split())
SPARSE_DIM = 1 << 20


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text) if t.lower() not in STOP]


def has_code(query: str) -> bool:
    return bool(_CODE.search(query))


def _h(token: str, mod: int) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big") % mod


@dataclass
class SparseVec:
    indices: list[int]
    values: list[float]


def sparse_vector(text: str, *, query: bool = False) -> SparseVec:
    """Term-frequency sparse vector (Qdrant applies IDF via ``modifier: idf``). Queries use binary weights."""
    tf: dict[int, float] = {}
    for tok in tokenize(text):
        i = _h(tok, SPARSE_DIM)
        tf[i] = tf.get(i, 0.0) + 1.0
    if query:
        tf = dict.fromkeys(tf, 1.0)
    else:  # BM25-style saturation (k1=1.2) so repetition does not dominate
        tf = {i: (v * 2.2) / (v + 1.2) for i, v in tf.items()}
    items = sorted(tf.items())
    return SparseVec([i for i, _ in items], [v for _, v in items])


# ---------------------------------------------------------------------------------------------
# hashing embedder: unigram + bigram + stem-ish prefix features. Deterministic, dependency-free; stands in for the gateway in tests.
# ---------------------------------------------------------------------------------------------
class HashEmbedder:
    model_id = "hash-embed@768"

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = tokenize(text)
        feats = list(toks) + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)] + [t[:5] + "*" for t in toks if len(t) > 5]
        for f in feats:
            h = _h(f, 1 << 32)
            v[h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        return [self._vec(t.removeprefix("search_query: ").removeprefix("search_document: ")) for t in texts]


# ---------------------------------------------------------------------------------------------
# PII (same shapes as the gateway guard; hospital case context must arrive masked)
# ---------------------------------------------------------------------------------------------
PII_PATTERNS = {
    "aadhaar": re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "mobile": re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "member_id_raw": re.compile(r"\bMEM-\d{8,}\b"),
}
_PLACEHOLDER = re.compile(r"<(PERSON|PHONE|ID|EMAIL|ADDRESS|AADHAAR|PAN|LOCATION|DATE_TIME)_\d+>")
_CURRENCY = re.compile(r"(?:₹|INR|Rs\.?|Rupees)\s*[:\-]?\s*$", re.IGNORECASE)


def pii_hits(text: str) -> set[str]:
    text = _PLACEHOLDER.sub("", text)
    hits: set[str] = set()
    for name, pat in PII_PATTERNS.items():
        for m in pat.finditer(text):
            if name in ("aadhaar", "mobile") and _CURRENCY.search(text[max(0, m.start() - 12) : m.start()]):
                continue
            hits.add(name)
            break
    return hits


def mask_pii(text: str) -> str:
    for name, pat in PII_PATTERNS.items():
        text = pat.sub(f"<{name.upper()}>", text)
    return text
