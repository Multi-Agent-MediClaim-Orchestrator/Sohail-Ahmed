"""Hard stop before any non-local model call (doc 02 §6.6)."""

from __future__ import annotations

import re

from docpipe.stages import entities

HARD = ("AADHAAR", "PAN", "PHONE", "EMAIL_ADDRESS", "ACCOUNT_NO", "IFSC")
TOKEN = re.compile(r"<[A-Z_]+_\d+>")


class GuardTripped(Exception):
    pass


def assert_safe(masked_text: str, pii_map: dict[str, str]) -> None:
    for raw in pii_map.values():
        if len(raw) >= 4 and raw in masked_text:
            raise GuardTripped("raw_value_present")
    clean = TOKEN.sub(" ", masked_text)
    for e in entities.find(clean):
        if e.type in HARD and e.score >= 0.5:
            raise GuardTripped(f"{e.type.lower()}_pattern_present")
