"""Nothing that looks like identity leaves for a model (doc 09 task 9). Runs on the final prompt payload."""

from __future__ import annotations

import re

PATTERNS = {
    "aadhaar": re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{9}(?!\d)"),
}


class PiiDetected(Exception):
    def __init__(self, kinds: list[str]) -> None:
        super().__init__("payload contains " + ", ".join(kinds))
        self.kinds = kinds


def scan(text: str) -> list[str]:
    return [k for k, rx in PATTERNS.items() if rx.search(text)]


def assert_no_pii(text: str) -> None:
    kinds = scan(text)
    if kinds:
        raise PiiDetected(kinds)
