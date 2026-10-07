"""Payload models for the insurer config domains (01-03 §4.2). ``schema_id = "<domain>@<n>"``."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from calc_engine.rules_schema import PolicyRules
from claim_contract.enums import DocType
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _dec(v: Any) -> Decimal:
    if isinstance(v, (float, bool)):
        raise ValueError("use strings for money")
    return v if isinstance(v, Decimal) else Decimal(str(v))


Money = Annotated[Decimal, BeforeValidator(_dec)]


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Thresholds(_P):
    schema_id: Literal["thresholds@1"] = "thresholds@1"
    t_auto_inr: Money = Decimal("50000")
    t_four_inr: Money = Decimal("500000")
    identity_min_score: float = Field(default=0.90, ge=0, le=1)
    authenticity_floor: float = Field(default=0.70, ge=0, le=1)
    authenticity_warn_floor: float = Field(default=0.85, ge=0, le=1)
    name_similarity_min: float = Field(default=0.88, ge=0, le=1)
    dob_must_match: bool = True
    # --- user decision: clean claims up to T_auto are approved without a human click -------------
    auto_approval_enabled: bool = True
    auto_min_identity_score: float = Field(default=0.90, ge=0, le=1)
    auto_max_utilisation_pct: int = Field(default=80, ge=1, le=100)
    review_flags: list[str] = Field(
        default_factory=lambda: [
            "overridden_blocker", "agent_deterministic_disagreement", "degraded_mode", "watchlist_hospital",
            "high_utilisation", "fraud_warning", "manual_increase", "escalated_case",
        ]
    )
    high_utilisation_pct: int = Field(default=80, ge=1, le=100)
    sla_hours: dict[str, int] = Field(
        default_factory=lambda: {"cashless_emergency": 4, "cashless_planned": 8, "reimbursement": 120}
    )

    @model_validator(mode="after")
    def _order(self) -> Thresholds:
        if self.t_auto_inr >= self.t_four_inr:
            raise ValueError("T_auto must be < T_four")
        return self


class QueryPolicy(_P):
    schema_id: Literal["query_policy@1"] = "query_policy@1"
    max_rounds: int = Field(default=3, ge=1, le=3)
    round_sla_hours: list[int] = Field(default_factory=lambda: [72, 48, 24])
    reminder_offsets_hours: list[int] = Field(default_factory=lambda: [24, 6])
    auto_send_categories: list[str] = Field(default_factory=lambda: ["missing_document"])
    max_queries_per_round: int = Field(default=1, ge=1)
    allowed_extension: int = Field(default=1, ge=0, le=1)
    escalation_role: str = "senior_reviewer"
    skip_weekends: bool = False
    auto_close_after_days: int = 30

    @model_validator(mode="after")
    def _len(self) -> QueryPolicy:
        if len(self.round_sla_hours) < self.max_rounds:
            raise ValueError("round_sla_hours needs one entry per round")
        return self


class ConditionalRule(_P):
    when: dict[str, Any] = Field(alias="if")
    require: list[str]
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DocRequirements(_P):
    schema_id: Literal["doc_requirements@1"] = "doc_requirements@1"
    required: list[DocType]
    optional: list[DocType] = Field(default_factory=list)
    conditional: list[ConditionalRule] = Field(default_factory=list)
    stamp_required_for: list[DocType] = Field(default_factory=lambda: [DocType.final_bill, DocType.itemised_bill, DocType.pharmacy_bill])
    min_parse_confidence: float = Field(default=0.80, ge=0, le=1)
    max_pages_per_doc: int = 300


class PolicyRulesPayload(_P):
    schema_id: Literal["policy_rules@1"] = "policy_rules@1"
    product_code: str
    reject_reasons: list[str] = Field(
        default_factory=lambda: ["EXCL_PREEXISTING", "EXCL_WAITING", "EXCL_COSMETIC", "POLICY_INACTIVE", "SI_EXHAUSTED",
                                 "DOCS_NOT_PROVIDED", "DUPLICATE_CLAIM", "AUTH_TAMPER"]
    )
    rules: PolicyRules


SCHEMAS: dict[str, type[BaseModel]] = {
    "thresholds@1": Thresholds,
    "query_policy@1": QueryPolicy,
    "doc_requirements@1": DocRequirements,
    "policy_rules@1": PolicyRulesPayload,
}
DOMAIN_SCHEMA = {d.split("@")[0]: d for d in SCHEMAS}
TWO_PERSON_DOMAINS = {"thresholds", "policy_rules", "exclusions"}
