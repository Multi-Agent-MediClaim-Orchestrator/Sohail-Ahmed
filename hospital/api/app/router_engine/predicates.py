"""Closed predicate language with three-valued logic (doc 05 §3.2, §5.1). No eval/exec/compile anywhere."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class Tri:
    v: bool | None  # True / False / None = UNKNOWN


TRUE, FALSE, UNKNOWN = Tri(True), Tri(False), Tri(None)


def and_(*xs: Tri) -> Tri:
    if any(x.v is False for x in xs):
        return FALSE
    if any(x.v is None for x in xs):
        return UNKNOWN
    return TRUE


def or_(*xs: Tri) -> Tri:
    if any(x.v is True for x in xs):
        return TRUE
    if any(x.v is None for x in xs):
        return UNKNOWN
    return FALSE


def not_(x: Tri) -> Tri:
    return UNKNOWN if x.v is None else Tri(not x.v)


SUFFIXES = ("_gte", "_lte", "_gt", "_lt")
SPECIAL_KEYS = {"default", "any", "all", "not", "or_doc_type_present", "icd10_prefix"}
KNOWN_FACTS = {
    "claim_type_proposed",
    "preauth_ref_present",
    "preauth_valid",
    "hospital_network",
    "admission_source",
    "admission_note_text_flags",
    "stay_days",
    "icd10_codes",
    "procedure_codes",
    "procedure_group",
    "claimed_amount",
    "doc_types_present",
    "preauth_issue",
    "admitted_at",
    "discharged_at",
}


def split_suffix(key: str) -> tuple[str, str]:
    for s in SUFFIXES:
        if key.endswith(s):
            return key[: -len(s)], s[1:]
    return key, "eq"


def _num(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise InvalidOperation
    return Decimal(str(v))


def compare(op: str, fact: Any, operand: Any) -> Tri:
    try:
        if op == "eq":
            if (
                isinstance(operand, dict) and "contains" in operand
            ):  # set facts: any of the values present
                return Tri(bool(set(operand["contains"]) & set(fact)))
            if isinstance(operand, list):
                return Tri(
                    fact in operand
                    if not isinstance(fact, (set, frozenset, list))
                    else bool(set(operand) & set(fact))
                )
            return Tri(fact == operand)
        a, b = _num(fact), _num(operand)
        return Tri({"gte": a >= b, "lte": a <= b, "gt": a > b, "lt": a < b}[op])
    except (InvalidOperation, TypeError, ValueError):
        return UNKNOWN


def holds(cond: dict[str, Any], facts: dict[str, Any]) -> Tri:
    if cond.get("default") is True:
        return TRUE
    results = [eval_key(k, v, facts) for k, v in cond.items() if k != "or_doc_type_present"]
    base = and_(*results) if results else TRUE
    if "or_doc_type_present" in cond:
        present = bool(set(cond["or_doc_type_present"]) & set(facts.get("doc_types_present") or ()))
        return or_(base, TRUE if present else FALSE)
    return base


def eval_key(key: str, operand: Any, facts: dict[str, Any]) -> Tri:
    if key in ("any", "all"):
        subs = [holds(c, facts) for c in operand]
        return or_(*subs) if key == "any" else and_(*subs)
    if key == "not":
        return not_(holds(operand, facts))
    if key == "icd10_prefix":
        codes = facts.get("icd10_codes")
        if codes is None:
            return UNKNOWN
        return Tri(any(str(c).upper().startswith(tuple(p.upper() for p in operand)) for c in codes))
    fact, op = split_suffix(key)
    if fact not in facts or facts[fact] is None:
        return UNKNOWN
    return compare(op, facts[fact], operand)


# ------------------------------------------------------------------------------------- validation
def validate_cond(cond: Any, path: str = "if") -> list[str]:
    errs: list[str] = []
    if not isinstance(cond, dict) or not cond:
        return [f"{path}: condition must be a non-empty object"]
    for key, operand in cond.items():
        if key == "default":
            if operand is not True:
                errs.append(f"{path}.default must be true")
        elif key in ("any", "all"):
            if not isinstance(operand, list) or not operand:
                errs.append(f"{path}.{key} needs a non-empty list")
            else:
                for i, c in enumerate(operand):
                    errs += validate_cond(c, f"{path}.{key}[{i}]")
        elif key == "not":
            errs += validate_cond(operand, f"{path}.not")
        elif key in ("or_doc_type_present", "icd10_prefix"):
            if not isinstance(operand, list) or not all(isinstance(x, str) for x in operand):
                errs.append(f"{path}.{key} needs a list of strings")
        else:
            fact, op = split_suffix(key)
            if fact not in KNOWN_FACTS:
                errs.append(f"{path}: unknown predicate key {key!r}")
            elif op != "eq" and (isinstance(operand, (list, dict)) or isinstance(operand, bool)):
                errs.append(f"{path}.{key} needs a number")
            elif op != "eq" and not isinstance(operand, (int, float, str)):
                errs.append(f"{path}.{key} needs a number")
            elif op == "eq" and isinstance(operand, dict) and set(operand) != {"contains"}:
                errs.append(f"{path}.{key}: only {{'contains': [...]}} objects are allowed")
    return errs
