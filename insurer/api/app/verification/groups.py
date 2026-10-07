"""Procedure-group derivation (drives doc requirements, sub-limits and specific waiting periods). Deterministic table."""

from __future__ import annotations

ICD_GROUPS: list[tuple[tuple[str, ...], str]] = [
    (("H25", "H26", "H28"), "cataract"),
    (("M17", "M16"), "knee_replacement"),
    (("K40", "K41", "K42", "K43", "K44", "K45", "K46"), "hernia"),
    (("O80", "O81", "O82", "O83", "O84", "O60"), "maternity"),
    (("K80", "K81", "K35", "K36", "K37"), "abdominal_surgery"),
    (("I20", "I21", "I22", "I25"), "cardiac"),
    (("N20", "N21"), "urology"),
    (("N18", "Z49"), "dialysis"),
]
PROCEDURE_GROUPS: list[tuple[tuple[str, ...], str]] = [
    (("0SR",), "knee_replacement"),
    (("08R", "08T"), "cataract"),
    (("0FT",), "abdominal_surgery"),
]


def derive_procedure_group(diagnosis_codes: list[str], procedure_codes: list[str]) -> str | None:
    for code in procedure_codes:
        for prefixes, group in PROCEDURE_GROUPS:
            if code.startswith(prefixes):
                return group
    for code in diagnosis_codes:
        for prefixes, group in ICD_GROUPS:
            if code.startswith(prefixes):
                return group
    return None


def specific_group(icd: str) -> str | None:
    """Group name used for specific-illness waiting periods (cataract, hernia, knee_replacement ...)."""
    return derive_procedure_group([icd], [])
