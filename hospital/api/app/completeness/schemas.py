"""Config and result schemas for the completeness engine (doc 04 §3)."""

from __future__ import annotations

from typing import Any, Literal

from claim_contract.enums import DocType
from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["info", "warning", "review", "blocker"]
ItemStatus = Literal[
    "present_ok",
    "missing",
    "unusable",
    "needs_review",
    "waived",
    "not_applicable",
    "pending_processing",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Applies(Strict):
    claim_type: list[str] | None = None
    admission_type: list[str] | None = None
    procedure_group: list[str] | None = None
    flags: list[str] | None = None


class Rule(Strict):
    id: str = Field(min_length=1, max_length=40)
    doc_type: DocType
    applies: Applies = Field(default_factory=Applies)
    requirement: Literal["required", "conditional", "optional"] = "required"
    min_parse_confidence: float | None = Field(default=None, ge=0, le=1)
    must_have_fields: list[str] = Field(default_factory=list, max_length=30)
    must_have_stamp: bool = False
    waivable: bool = False
    any_candidate: bool = False
    condition: str | None = None


class Alternative(Strict):
    any_of: list[DocType] | None = None
    all_of: list[DocType] | None = None
    target: str = Field(alias="for")

    @model_validator(mode="after")
    def _one_kind(self) -> Alternative:
        if bool(self.any_of) == bool(self.all_of):
            raise ValueError("alternative needs exactly one of any_of / all_of")
        return self

    @property
    def rule_id(self) -> str:
        return self.target.removesuffix("-alt")


class Ordering(Strict):
    chronological: bool = False
    by: str = "document_date"
    rule_id: str = "R-ORD-01"
    severity: Literal["warning", "review"] = "warning"


class StampRule(Strict):
    min_stamp_confidence: float = Field(default=0.7, ge=0, le=1)


class DocRequirementsConfig(Strict):
    rules: list[Rule] = Field(min_length=1)
    alternatives: list[Alternative] = Field(default_factory=list)
    ordering: Ordering | None = None
    procedure_groups: dict[str, list[str]] = Field(default_factory=dict)
    stamp_rules: dict[DocType, StampRule] = Field(default_factory=dict)


class Item(BaseModel):
    rule_id: str
    doc_type: str | None = None
    requirement: str = "required"
    status: ItemStatus
    severity: Severity = "info"
    reasons: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    message: str = ""
    condition: str | None = None
    via: str | None = None


class Result(BaseModel):
    complete: bool
    provisional: bool
    items: list[Item]

    @property
    def blocker_count(self) -> int:
        return sum(1 for i in self.items if i.severity == "blocker")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.items if i.severity == "warning")

    def summary(self) -> dict[str, int]:
        return {
            "blockers": self.blocker_count,
            "warnings": self.warning_count,
            "needs_review": sum(1 for i in self.items if i.status == "needs_review"),
            "ok": sum(1 for i in self.items if i.status == "present_ok"),
            "waived": sum(1 for i in self.items if i.status == "waived"),
        }


def semantic_errors(cfg: DocRequirementsConfig) -> list[str]:
    """Checks beyond the JSON schema (doc 04 task 13)."""
    errs: list[str] = []
    ids = [r.id for r in cfg.rules]
    errs += [f"duplicate rule id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    known = set(ids)
    for a in cfg.alternatives:
        if a.rule_id not in known:
            errs.append(f"alternative targets unknown rule {a.target}")
    groups = set(cfg.procedure_groups)
    for r in cfg.rules:
        for g in r.applies.procedure_group or []:
            if g != "*" and g not in groups:
                errs.append(f"rule {r.id} references undefined procedure group {g}")
        if r.requirement == "conditional" and r.applies == Applies():
            errs.append(f"conditional rule {r.id} has no applicability condition")
    if cfg.ordering and cfg.ordering.rule_id in known:
        errs.append(f"ordering rule id {cfg.ordering.rule_id} collides with a rule")
    return errs


def validate_payload(payload: dict[str, Any]) -> tuple[DocRequirementsConfig | None, list[str]]:
    from pydantic import ValidationError

    try:
        cfg = DocRequirementsConfig.model_validate(payload)
    except ValidationError as e:
        return None, [f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors()]
    return cfg, semantic_errors(cfg)
