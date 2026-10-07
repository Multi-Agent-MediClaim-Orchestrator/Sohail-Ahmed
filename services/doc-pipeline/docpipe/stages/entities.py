"""S4: find identifiers. Custom Indian-ID recognizers (with checksums) plus Presidio's spaCy PERSON and EMAIL.
Allowlist keeps clinical and billing vocabulary readable for the model (doctor names, ICD codes, drug names)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
      [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]  # fmt: skip
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
      [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]  # fmt: skip


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(re.sub(r"\D", "", num))):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


class Aadhaar(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="AADHAAR",
            patterns=[Pattern("aadhaar", r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)", 0.5)],
            context=["aadhaar", "uid", "आधार"],
        )

    def validate_result(self, pattern_text: str) -> bool | None:
        return (
            True if verhoeff_ok(pattern_text) else None
        )  # keep unverified ones at low score: still masked with context


def _rec(entity: str, regex: str, score: float, ctx: list[str] | None = None) -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity=entity, patterns=[Pattern(entity.lower(), regex, score)], context=ctx or []
    )


ALLOW_PREFIX = re.compile(r"(?i)\b(dr|prof|mr|mrs|ms)\.?\s*$")
ALLOW_WORDS = {
    "hospital",
    "clinic",
    "medical",
    "centre",
    "center",
    "pharmacy",
    "laboratory",
    "diagnostics",
}


@lru_cache
def analyzer() -> AnalyzerEngine:
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()
    a = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    for r in (
        Aadhaar(),
        _rec("PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b", 0.9),
        _rec("PHONE", r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)", 0.8),
        _rec("IFSC", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.85),
        _rec("ACCOUNT_NO", r"(?<!\d)\d{9,18}(?!\d)", 0.3, ["a/c", "account", "acc no", "a/c no"]),
        _rec("POLICY_NO", r"\b(?:policy|pol)[\s.:#no-]*([A-Z0-9][A-Z0-9/-]{5,19})\b", 0.6),
        _rec("MEMBER_ID", r"\b(?:member|mem)[\s.:#id-]*([A-Z0-9][A-Z0-9/-]{3,19})\b", 0.6),
        _rec(
            "DOB", r"(?i)(?:dob|date of birth|born on)[\s:.-]*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", 0.85
        ),
    ):
        a.registry.add_recognizer(r)
    return a


@dataclass(frozen=True)
class Ent:
    type: str
    start: int
    end: int
    score: float


MASK_TYPES = [
    "AADHAAR",
    "PAN",
    "PHONE",
    "IFSC",
    "ACCOUNT_NO",
    "POLICY_NO",
    "MEMBER_ID",
    "DOB",
    "EMAIL_ADDRESS",
    "PERSON",
]
MIN_SCORE = {"ACCOUNT_NO": 0.5, "PERSON": 0.6, "AADHAAR": 0.4}


LABELLED_NAME = re.compile(
    r"(?im)\b(?:patient(?:\s+name)?|pt\.?\s*name|name|account\s+holder|member\s+name|received\s+from|mr\.?|mrs\.?|ms\.?)\s*[:\-.]?\s*"
    r"([A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+){0,3})"
)


def find(text: str) -> list[Ent]:
    res: list[RecognizerResult] = analyzer().analyze(text=text, language="en", entities=MASK_TYPES)
    out: list[Ent] = []
    for r in res:
        if r.score < MIN_SCORE.get(r.entity_type, 0.5):
            continue
        span = text[r.start : r.end]
        if r.entity_type == "PERSON":
            if ALLOW_PREFIX.search(text[max(0, r.start - 6) : r.start]) or any(
                w in span.lower() for w in ALLOW_WORDS
            ):
                continue  # treating doctors and facility names stay readable
            cut = re.split(r"\s{2,}|\t|\n|[:/|]", span, maxsplit=1)[
                0
            ].rstrip()  # a name never spans a column gap or label
            if not cut:
                continue
            out.append(Ent("PERSON", r.start, r.start + len(cut), r.score))
            continue
        if r.entity_type in ("POLICY_NO", "MEMBER_ID"):  # keep only the id part, not the label
            m = re.search(r"[A-Z0-9][A-Z0-9/-]{3,19}$", span)
            if not m:
                continue
            out.append(Ent(r.entity_type, r.start + m.start(), r.end, r.score))
            continue
        out.append(Ent(r.entity_type, r.start, r.end, r.score))
    for m in LABELLED_NAME.finditer(
        text
    ):  # spaCy misses names in form-style "Patient Name: ..." lines
        if not any(w in m.group(1).lower() for w in ALLOW_WORDS):
            out.append(Ent("PERSON", m.start(1), m.end(1), 0.85))
    for name in {
        text[e.start : e.end] for e in out if e.type == "PERSON" and len(text[e.start : e.end]) >= 4
    }:
        for m in re.finditer(
            re.escape(name), text
        ):  # the same person elsewhere on the page, whatever NER said
            out.append(Ent("PERSON", m.start(), m.end(), 0.8))
    out.sort(key=lambda e: (e.start, -(e.end - e.start)))
    merged: list[Ent] = []
    for e in out:  # drop overlaps, keep the longest/first
        if merged and e.start < merged[-1].end:
            continue
        merged.append(e)
    return merged
