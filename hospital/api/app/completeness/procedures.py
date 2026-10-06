"""Procedure grouping and derived case flags."""

from __future__ import annotations

from fnmatch import fnmatchcase


def derive_procedure_group(codes: list[str], groups: dict[str, list[str]]) -> str | None:
    """First group (sorted by name for determinism) whose glob patterns match any procedure code."""
    for name in sorted(groups):
        if any(fnmatchcase(c.upper(), pat.upper()) for c in codes for pat in groups[name]):
            return name
    return None


def has_surgery(codes: list[str]) -> bool:
    """ICD-10-PCS section 0 is Medical and Surgical procedures."""
    return any(c.strip().startswith("0") for c in codes)
