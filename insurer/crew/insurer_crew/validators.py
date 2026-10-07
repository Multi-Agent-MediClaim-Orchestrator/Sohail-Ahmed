"""Code validators that run on every agent input/output (03-08 §3.4, §6.2, §6.4, §6.6, §8). Agents propose; these dispose."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from pydantic import BaseModel

# ------------------------------------------------------------------------------------------------ PII (§6.6)
_MASKED = re.compile(r"\[[A-Z_]+_\d+\]|<[A-Z_]+_\d+>|X{2,}\d{2,4}|\*{2,}\d{2,4}|MEM-\*+\d+")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_CURRENCY_BEFORE = re.compile(r"(?:₹|inr|rs\.?|rupees|qty|×|x)\s*[:\-]?\s*$", re.IGNORECASE)
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "aadhaar": re.compile(r"(?<![\d.])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?![\d])"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "phone": re.compile(r"(?<![\d.])(?:\+?91[\s-]?)?[6-9]\d{9}(?![\d])"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "upi": re.compile(r"\b[\w.-]{2,}@(?:ok\w+|ybl|paytm|upi|apl|axl|ibl|sbi|hdfcbank|icici|axisbank)\b", re.IGNORECASE),
    "member_id_raw": re.compile(r"\bMEM-\d{8,}\b"),  # raw member ids never leave the insurer; masked form is MEM-****1234
    "bank_account": re.compile(r"(?<![\d.,])\d{9,18}(?![\d,])"),
}


def pii_kinds(text: str) -> set[str]:
    """Pattern names present in ``text`` (masked tokens, UUIDs and currency/quantity-prefixed numbers are not PII)."""
    clean = _UUID.sub(" ", _MASKED.sub(" ", text))
    kinds: set[str] = set()
    for name, pat in PII_PATTERNS.items():
        for m in pat.finditer(clean):
            if name in ("aadhaar", "phone", "bank_account") and _CURRENCY_BEFORE.search(clean[max(0, m.start() - 10) : m.start()]):
                continue
            if name == "bank_account" and re.fullmatch(r"(?:19|20)\d{6,}", m.group()) and len(m.group()) in (9, 10):
                continue
            kinds.add(name)
            break
    # a 12-digit run is reported as Aadhaar before it can be reported as an account
    if "aadhaar" in kinds:
        kinds.discard("bank_account")
    return kinds


def iter_strings(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")
    elif isinstance(obj, BaseModel):
        yield from iter_strings(obj.model_dump(mode="json"), path)


def scan_input(obj: Any) -> dict[str, set[str]]:
    """Field path -> PII kinds found. Non-empty means an API bug (400 ``pii_in_context``)."""
    hits: dict[str, set[str]] = {}
    for p, s in iter_strings(obj):
        k = pii_kinds(s)
        if k:
            hits[p] = k
    return hits


def redact(text: str) -> tuple[str, set[str]]:
    kinds = pii_kinds(text)
    out = text
    for k in kinds:
        out = PII_PATTERNS[k].sub(f"[{k.upper()}_REDACTED]", out)
    return out, kinds


def redact_model(model: BaseModel) -> tuple[BaseModel, list[str]]:
    """Redact PII in every string of an output model (round-trip through JSON); returns the model and warnings."""
    data = model.model_dump(mode="json")
    warnings: list[str] = []

    def walk(v: Any, path: str) -> Any:
        if isinstance(v, str):
            new, kinds = redact(v)
            if kinds:
                warnings.append(f"pii_redacted:{path}:{','.join(sorted(kinds))}")
            return new
        if isinstance(v, dict):
            return {k: walk(x, f"{path}.{k}" if path else k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{path}[{i}]") for i, x in enumerate(v)]
        return v

    return type(model).model_validate(walk(data, "")), warnings


# ------------------------------------------------------------------------------------------------ forbidden phrases / tone (rule 7)
FORBIDDEN = [
    "will be approved", "guaranteed", "we will pay", "claim is approved", "claim is rejected", "claim will be approved", "claim will be rejected", "has been approved",
    "has been rejected", "we guarantee", "assured of payment", "will definitely be paid", "payment is assured",
]
ACCUSATORY = {"forged": "inconsistent", "forgery": "inconsistency", "fake": "unverified", "fraudulent": "irregular", "fraud": "irregularity", "cheating": "irregularity", "liar": "inconsistent",
              "scam": "irregularity", "criminal": "irregular"}
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text) if s.strip()]


def strip_forbidden(text: str) -> tuple[str, list[str]]:
    """Drop every sentence that contains a forbidden phrase; returns (clean text, removed phrases)."""
    kept, removed = [], []
    for line in text.split("\n"):
        out = []
        for s in re.split(r"(?<=[.!?])\s+", line):
            low = s.lower()
            hit = [p for p in FORBIDDEN if p in low]
            if hit:
                removed += hit
            else:
                out.append(s)
        kept.append(" ".join(out))
    return "\n".join(kept).strip(), removed


def neutralise(text: str) -> tuple[str, list[str]]:
    """Replace accusatory words with neutral ones (authenticity/supervisor)."""
    swapped: list[str] = []
    for bad, good in ACCUSATORY.items():
        pat = re.compile(rf"\b{bad}\w*\b", re.IGNORECASE)
        if pat.search(text):
            swapped.append(bad)
            text = pat.sub(good, text)
    return text, swapped


_IMPERATIVE_OK = re.compile(r"\b(please|kindly|request|thank)\b", re.IGNORECASE)


def tone_check(text: str) -> dict[str, bool]:
    low = text.lower()
    return {"polite": bool(_IMPERATIVE_OK.search(low)) and not re.search(r"\b(you must|immediately|or else|your fault)\b", low),
            "no_accusation": not any(re.search(rf"\b{w}\w*\b", low) for w in ACCUSATORY),
            "no_promise": not any(p in low for p in FORBIDDEN)}


# ------------------------------------------------------------------------------------------------ amounts (rule 2)
_CUR = re.compile(r"(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:rupees|/-)", re.IGNORECASE)
_PAYABLE = re.compile(r"\b(?:payable|approved|approve|settle\w*|reimburs\w*|pay(?:ing)?|payout|admissible)\b[^.\n]{0,30}?(\d[\d,]{3,}(?:\.\d+)?)", re.IGNORECASE)


def _num(s: str) -> str:
    s = s.replace(",", "").rstrip(".")
    return s[:-3] if s.endswith(".00") else s


def allowed_amounts(*sources: Any) -> set[str]:
    """Every number that appears in the request context (bill lines, finding details): quoting those is allowed."""
    out: set[str] = set()
    for src in sources:
        for _, s in iter_strings(src):
            out.update(_num(m) for m in re.findall(r"\d[\d,]*(?:\.\d+)?", s))
        if isinstance(src, dict | list):
            stack = [src]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
                elif isinstance(cur, int | float) and not isinstance(cur, bool):
                    out.add(_num(str(cur)))
    return out


def reject_payables(text: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Drop sentences that state a currency amount or a payable/approved figure not present in the context."""
    kept, dropped = [], []
    for line in text.split("\n"):
        out = []
        for s in re.split(r"(?<=[.!?])\s+", line):
            nums = [m.group(1) or m.group(2) for m in _CUR.finditer(s)] + [m.group(1) for m in _PAYABLE.finditer(s)]
            bad = [n for n in nums if _num(n) not in allowed]
            if bad:
                dropped.append(s.strip())
            else:
                out.append(s)
        kept.append(" ".join(out))
    return "\n".join(kept).strip(), dropped


# ------------------------------------------------------------------------------------------------ citations (§6.2)
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%.]", " ", s.lower())).strip()


def quote_in(quote: str, chunk_text: str) -> bool:
    q = norm(quote)
    return bool(q) and q in norm(chunk_text)


# ------------------------------------------------------------------------------------------------ JSON extraction / repair (§6.4)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> str:
    t = _FENCE.sub("", text.strip()).strip()
    start = t.find("{")
    if start < 0:
        return t
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                t = t[start : i + 1]
                break
    else:
        t = t[start:] + "}" * max(depth, 0)  # truncated output: close the braces
    return re.sub(r",(\s*[}\]])", r"\1", t)  # trailing commas


def parse_json(text: str) -> Any:
    return json.loads(extract_json(text))


# ------------------------------------------------------------------------------------------------ prompt injection (§8)
INJECTION_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)", r"disregard (?:all |any |the )?(?:previous|prior|above|your) (?:instructions?|rules?)",
    r"you are now\b", r"new instructions?:", r"system prompt", r"</?\s*system\s*>", r"(?:approve|accept|pay|settle) (?:this|the|every|all) claims?", r"mark (?:this |the )?claim as (?:approved|settled|paid)",
    r"do not (?:flag|report|mention)", r"reveal (?:your|the) (?:prompt|instructions)", r"override (?:the )?(?:checks?|rules?|validation)", r"automated systems?:",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


def injection_suspected(text: str) -> bool:
    return bool(_INJ.search(text))


def strip_injection(text: str) -> tuple[str, int]:
    """Remove instruction-like sentences from a model output field; returns (clean, removed count)."""
    kept, n = [], 0
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        if _INJ.search(s):
            n += 1
        elif s.strip():
            kept.append(s.strip())
    return " ".join(kept), n


def wrap_untrusted(label: str, text: str) -> str:
    safe = text.replace("</document>", "< /document>")
    return f'<document untrusted label="{label}">\n{safe}\n</document>'


Mapper = Callable[[str], str]
