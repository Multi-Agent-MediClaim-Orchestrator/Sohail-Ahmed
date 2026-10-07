"""Deterministic, read-only tools (03-08 §3.1, task 6). None of these call an LLM and none can change state."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "kumari", "master", "baby", "prof", "late", "mohd", "md"}
# common Indian transliteration variants folded to one spelling before comparison
_TRANSLIT = [("mohammad", "mohammed"), ("muhammad", "mohammed"), ("mohd", "mohammed"), ("kumar", "kumar"), ("sh", "s"), ("ee", "i"), ("oo", "u"), ("w", "v"), ("ph", "f"), ("th", "t"), ("dh", "d"), ("kh", "k"), ("y", "i")]


def normalise_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(t for t in s.split() if t not in HONORIFICS)


def fold(name: str) -> str:
    out = normalise_name(name)
    for a, b in _TRANSLIT:
        out = out.replace(a, b)
    return out


def _initials_expanded(a: list[str], b: list[str]) -> bool:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    used: set[int] = set()
    expanded = False
    for t in short:
        for i, u in enumerate(long_):
            if i in used:
                continue
            if t == u:
                used.add(i)
                break
            if (len(t) == 1 and u.startswith(t)) or (len(u) == 1 and t.startswith(u)):
                used.add(i)
                expanded = True
                break
        else:
            return False
    return expanded


def compare_names(variant: str, member: str) -> dict[str, Any]:
    """Similarity of a document name vs the member-record name: token-set + Jaro-Winkler (+ transliteration folding, initials expansion)."""
    a, b = normalise_name(variant), normalise_name(member)
    if not a or not b:
        return {"score": 0.0, "method": "empty", "tokens_missing": [], "initials_expanded": False}
    if a == b:
        return {"score": 1.0, "method": "exact", "tokens_missing": [], "initials_expanded": False}
    fa, fb = fold(variant), fold(member)
    cands = {"jaro_winkler": JaroWinkler.similarity(a, b), "token_set": fuzz.token_set_ratio(a, b) / 100, "token_sort": fuzz.token_sort_ratio(a, b) / 100,
             "translit": max(JaroWinkler.similarity(fa, fb), fuzz.token_set_ratio(fa, fb) / 100)}
    method = max(cands, key=lambda k: cands[k])
    score = cands[method]
    ta, tb = a.split(), b.split()
    initials = _initials_expanded(ta, tb)
    if initials:
        score, method = max(score, 0.95), "initials"
    if sorted(ta) == sorted(tb):  # transposed order is a variation, not a mismatch
        score, method = max(score, 0.97), "reordered"
    missing = [t for t in tb if t not in ta and not any(len(x) == 1 and t.startswith(x) for x in ta)]
    return {"score": round(float(score), 3), "method": method, "tokens_missing": missing, "initials_expanded": initials}


# ------------------------------------------------------------------------------------------------ arithmetic
def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def get_bill_arithmetic(lines: list[dict[str, Any]], claimed_total: Any = None) -> dict[str, Any]:
    """qty x unit_price vs amount per line (tolerance 0.01) and the sum of lines vs a claimed total."""
    bad = []
    total = Decimal(0)
    for ln in lines:
        qty, up, amt = _d(ln.get("qty", 1)), _d(ln.get("unit_price", 0)), _d(ln.get("amount", 0))
        total += amt
        if abs(qty * up - amt) > Decimal("0.01"):
            bad.append({"line_ref": ln.get("line_ref"), "expected": str((qty * up).quantize(Decimal("0.01"))), "billed": str(amt.quantize(Decimal("0.01"))), "qty": str(qty), "unit_price": str(up)})
    out: dict[str, Any] = {"line_mismatches": bad, "lines_total": str(total.quantize(Decimal("0.01")))}
    if claimed_total is not None:
        diff = total - _d(claimed_total)
        out["total_mismatch"] = abs(diff) > Decimal("0.01")
        out["total_diff"] = str(diff.quantize(Decimal("0.01")))
    return out


def find_duplicates(report: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise the duplicate report the API computed (hash/near-dup across cases)."""
    r = report or {}
    return {"exact": list(r.get("exact", []) or r.get("duplicate_doc_ids", [])), "near": list(r.get("near", [])), "any": bool(r.get("exact") or r.get("near") or r.get("duplicate_doc_ids"))}


# ------------------------------------------------------------------------------------------------ ICD
ICD_TABLE = {
    "I21": "Acute myocardial infarction", "I25": "Chronic ischaemic heart disease", "K35": "Acute appendicitis", "K40": "Inguinal hernia", "K80": "Cholelithiasis",
    "M17": "Osteoarthritis of knee", "H25": "Senile cataract", "O80": "Single spontaneous delivery", "O82": "Delivery by caesarean section", "A01": "Typhoid and paratyphoid fevers",
    "A90": "Dengue fever", "J18": "Pneumonia, unspecified organism", "N20": "Calculus of kidney and ureter", "N85": "Other non-inflammatory disorders of uterus", "S72": "Fracture of femur",
    "J35": "Chronic diseases of tonsils and adenoids", "E11": "Type 2 diabetes mellitus", "I10": "Essential hypertension", "Z51": "Other medical care (chemotherapy)", "Z49": "Care involving dialysis",
}


def icd_lookup(code: str) -> dict[str, str] | None:
    c = code.strip().upper()
    name = ICD_TABLE.get(c.split(".")[0])
    return {"code": c, "name": name} if name else None


# ------------------------------------------------------------------------------------------------ rule-first line mapping
def keyword_map(category: str, description: str) -> dict[str, Any] | None:
    """Shared with the calc engine (07 §5 task 7): one rule table, no drift between mapper and engine."""
    from calc_engine.mapping import map_line

    m = map_line(category, description)
    if m is None:
        return None
    return {"mapped_group": m.mapped_group.value, "tags": list(m.tags), "is_non_medical": m.is_non_medical, "is_implant": m.is_implant, "source": "rule"}


# ------------------------------------------------------------------------------------------------ RAG (read-only)
@dataclass
class Chunk:
    chunk_id: str
    text: str
    doc_title: str = ""
    section: str | None = None
    score: float = 0.0
    policy_product: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None


class RagClientProto(Protocol):
    async def search(self, collection: str, query: str, filters: dict[str, Any], top_k: int = 4, *, case_id: str | None = None) -> list[Chunk]: ...


class RagUnavailable(Exception):
    pass


class RagClient:
    """HTTP client for rag-service ``/v1/search`` (read only; no write methods exist on purpose)."""

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20) -> None:
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, transport=transport, timeout=timeout)

    async def search(self, collection: str, query: str, filters: dict[str, Any], top_k: int = 4, *, case_id: str | None = None) -> list[Chunk]:
        body = {"collection": collection, "query": query, "filters": filters, "top_k": top_k, "rerank": True, "metadata": {"agent": "coverage", "case_id": case_id}}
        try:
            r = await self.http.post("/v1/search", json=body)
        except httpx.HTTPError as e:
            raise RagUnavailable(str(e)) from e
        if r.status_code >= 400:
            raise RagUnavailable(f"rag-service {r.status_code}")
        return [Chunk(x["citation_id"], x["text"], x.get("source") or "", x.get("section"), float(x.get("score", 0)), x.get("policy_product"), x.get("effective_from"), x.get("effective_to"))
                for x in r.json().get("results", [])]
