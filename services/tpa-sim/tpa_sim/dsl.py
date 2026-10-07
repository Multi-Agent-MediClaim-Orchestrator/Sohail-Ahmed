"""Scenario DSL (04-06 §4): a scenario is an ordered list of steps; each step fires one callback after ``after`` seconds.

Step kinds: ``status`` (insurer-status change), ``query`` (raise a query), ``decision`` (final decision), ``settlement``
(paid / reversed notice), ``wait_response`` (hold until the hospital answers the open query)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_DUR = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$")


def parse_duration(v: str | int | float) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(v.strip())
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return float(m.group(1)) * {"ms": 0.001, None: 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["status", "query", "decision", "settlement", "wait_response"]
    after: float = 0.0
    status: str | None = None  # InsurerCaseStatus value for kind=status
    note: str | None = None
    category: str = "missing_document"
    text: str = "Please provide the requested information so we can continue processing this claim."
    requested_doc_types: list[str] = Field(default_factory=list)
    due_in: float = 72 * 3600
    outcome: Literal["approve", "partial", "reject"] = "approve"
    approved_ratio: float = 1.0  # of claimed amount for partial
    reason_codes: list[str] = Field(default_factory=list)
    settle: Literal["paid", "reversed"] = "paid"
    mode: Literal["NEFT", "RTGS"] = "NEFT"
    on_timeout: float | None = None  # wait_response: give up after N seconds (-> next step)
    manual: bool = False  # only fires through /sim/claims/{ref}/fire-next
    signing: Literal["ok", "bad_secret", "skew_plus_10m"] = "ok"  # probe steps are expected to be rejected by the hospital
    duplicate: bool = False  # send the very same message twice (same sequence + idempotency key)

    @model_validator(mode="before")
    @classmethod
    def _dur(cls, d: Any) -> Any:
        if isinstance(d, dict):
            d = dict(d)
            for k in ("after", "due_in", "on_timeout"):
                if k in d and d[k] is not None:
                    d[k] = parse_duration(d[k])
        return d

    @model_validator(mode="after")
    def _need(self) -> Step:
        if self.kind == "status" and not self.status:
            raise ValueError("status step needs `status`")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = ""
    description: str = ""
    match: dict[str, Any] = Field(default_factory=dict)  # e.g. {"patient_name": "Test*"} or {"claimed_amount_gt": 100000}
    steps: list[Step]

    @model_validator(mode="after")
    def _unique(self) -> Scenario:
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step id")
        if self.steps and self.steps[-1].kind == "wait_response":
            raise ValueError("a scenario cannot end with wait_response")
        return self


def parse_scenario(text: str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(text))


def load_dir(path: str | Path) -> list[Scenario]:
    p = Path(path)
    return [parse_scenario(f.read_text(encoding="utf-8")) for f in sorted(p.glob("*.y*ml"))] if p.is_dir() else []


def matches(sc: Scenario, sub: dict[str, Any]) -> bool:
    """Optional routing rules: ``claimed_amount_gt/lt`` and ``*_glob`` on payload fields."""
    from fnmatch import fnmatch

    amt = float(sub.get("totals", {}).get("claimed", {}).get("amount", 0) or sub.get("claimed_amount", {}).get("amount", 0) or 0)
    for k, v in sc.match.items():
        if k == "claimed_amount_gt" and not amt > float(v):
            return False
        if k == "claimed_amount_lt" and not amt < float(v):
            return False
        if k in ("claim_type", "hospital_id"):
            if str(sub.get(k, "")) != str(v):
                return False
        if k == "patient_name":
            name = str(sub.get("patient", {}).get("name", ""))
            if not fnmatch(name, str(v)):
                return False
    return bool(sc.match)
