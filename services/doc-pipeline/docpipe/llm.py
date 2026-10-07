from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx


class LLMUnavailable(Exception):
    pass


class LLM(Protocol):
    async def json(self, model: str, prompt: str) -> dict[str, Any]: ...


class Ollama:
    def __init__(
        self, base: str, timeout: float = 180.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self.base, self.c = base.rstrip("/"), client or httpx.AsyncClient(timeout=timeout)

    async def json(self, model: str, prompt: str) -> dict[str, Any]:
        msgs = [{"role": "user", "content": prompt}]
        for _ in range(3):
            try:
                r = await self.c.post(
                    f"{self.base}/chat/completions",
                    json={
                        "model": model,
                        "messages": msgs,
                        "temperature": 0,
                        "stream": False,
                        "response_format": {"type": "json_object"},
                        "reasoning_effort": "none",  # thinking models: 2.7x faster, same JSON (measured)
                    },
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMUnavailable(type(e).__name__) from e
            content = r.json()["choices"][0]["message"]["content"] or ""
            try:
                obj = json.loads(content)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
            msgs = [
                *msgs,
                {"role": "assistant", "content": content[:1500]},
                {"role": "user", "content": "Reply with the JSON object only."},
            ]
        raise LLMUnavailable("invalid_json")


class Fake:
    """Test double. `script[model]` is a dict (or list consumed in order) returned for that model alias."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script, self.calls = script, []

    async def json(self, model: str, prompt: str) -> dict[str, Any]:
        self.calls.append((model, prompt))
        r = self.script.get(model)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if r is None or isinstance(r, Exception):
            raise (r if isinstance(r, Exception) else LLMUnavailable("no script"))
        return r  # type: ignore[no-any-return]


# ---- deterministic extractor ---------------------------------------------------------------------------------
_DATE = r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})"
_AMT = r"((?:Rs\.?\s*)?\d[\d,]*(?:\.\d{1,2})?)"
RULES: dict[str, re.Pattern[str]] = {
    "patient_name": re.compile(r"(?im)patient\s*name\s*[:\-]\s*(\S+(?: \S+){0,3})"),
    "name": re.compile(r"(?im)^\s*name\s*[:\-]\s*(\S+(?: \S+){0,3})"),
    "account_holder": re.compile(r"(?im)account\s*holder\s*[:\-]\s*(\S+(?: \S+){0,3})"),
    "doctor_name": re.compile(r"(?im)(?:treating\s*doctor\s*[:\-]\s*)?(Dr\.?\s+\S+(?: \S+)?)"),
    "bill_number": re.compile(r"(?im)bill\s*no\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{3,})"),
    "receipt_no": re.compile(r"(?im)receipt\s*no\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{3,})"),
    "date": re.compile(r"(?im)(?<![a-z])date\s*[:\-]\s*" + _DATE),
    "admitted_on": re.compile(r"(?im)date\s*of\s*admission\s*[:\-]?\s*" + _DATE),
    "discharged_on": re.compile(r"(?im)date\s*of\s*discharge\s*[:\-]?\s*" + _DATE),
    "dob": re.compile(r"(?im)(?:dob|date\s*of\s*birth)\s*[:\-]?\s*" + _DATE),
    "valid_till": re.compile(r"(?im)valid\s*(?:till|upto|until)\s*[:\-]?\s*" + _DATE),
    "total": re.compile(r"(?im)^\s*(?:grand\s*)?total\b\D{0,90}?" + _AMT + r"\s*$"),
    "discounts": re.compile(r"(?im)^\s*discount\b\D{0,90}?" + _AMT + r"\s*$"),
    "approved_amount": re.compile(r"(?im)approved\s*amount\D{0,10}" + _AMT),
    "claim_amount": re.compile(r"(?im)claim\s*amount\D{0,10}" + _AMT),
    "amount": re.compile(r"(?im)amount\s*[:\-]\s*" + _AMT),
    "mrp": re.compile(r"(?im)mrp\s*[:\-]?\s*" + _AMT),
    "serial_no": re.compile(r"(?im)serial\s*no\.?\s*[:\-]?\s*([A-Z0-9]{4,})"),
    "preauth_ref": re.compile(
        r"(?im)pre-?auth(?:\s*(?:ref|no))?\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{4,})"
    ),
    "diagnosis": re.compile(r"(?im)diagnosis\s*[:\-]\s*([A-Za-z][A-Za-z ]+?)(?:\s{2,}|\s+ICD|$)"),
    "provisional_diagnosis": re.compile(r"(?im)provisional\s*diagnosis\s*[:\-]\s*(.+)$"),
    "impression": re.compile(r"(?im)impression\s*[:\-]\s*(.+)$"),
    "hospital_name": re.compile(r"(?m)\A\s*(.+)$"),
    "lab_name": re.compile(r"(?im)^(.*(?:laborator|diagnostic).*)$"),
    "insurer_name": re.compile(r"(?im)insurer\s*[:\-]\s*(.+)$"),
    "id_type": re.compile(r"(?i)\b(aadhaar|pan|passport|voter)\b"),
    "implant_name": re.compile(r"(?im)implant\s*[:\-]\s*(.+?)(?:\s{2,}|$)"),
    "manufacturer": re.compile(r"(?im)manufacturer\s*[:\-]\s*(.+?)(?:\s{2,}|$)"),
}
_ICD = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b")


class RulesLLM:
    """Deterministic stand-in for the model: reads "Label: value" lines and amounts that are already printed in the
    (masked) text. Same interface as Ollama; used when no model is reachable (DOCPIPE_LLM=rules) and by the fast
    end-to-end run. It never invents values: a field with no matching line is null, so the evidence check passes by
    construction and the gate still sees missing fields."""

    async def json(self, model: str, prompt: str) -> dict[str, Any]:
        m = re.search(r"Fields: (.+)\n<doc>\n(.*)\n</doc>", prompt, re.S)
        if not m:
            return {}
        names = [x.split(" (")[0].strip() for x in m.group(1).split(", ")]
        text = m.group(2)
        out: dict[str, Any] = {}
        for n in names:
            if n == "medicines":
                meds = [x.strip() for x in re.findall(r"(?m)^\s*\d+\.\s*(.+?)\s{2,}", text)]
                out[n] = {"value": meds or None, "quote": "; ".join(meds)}
                continue
            if n == "icd_codes":
                found = list(dict.fromkeys(_ICD.findall(text)))
                out[n] = {"value": found or None, "quote": ", ".join(found)}
                continue
            rx = RULES.get(n)
            hit = rx.search(text) if rx else None
            v = (hit.group(1) if hit and hit.groups() else None) if hit else None
            if v is not None:
                v = re.split(r"\s{2,}", v.strip())[0].strip()
            out[n] = {"value": v or None, "quote": v or ""}
        return out
