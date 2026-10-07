"""``PolicyRules`` (07 §3.1). Strict: unknown keys rejected, missing required keys rejected, floats rejected."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

SUPPORTED_SCHEMA_VERSIONS = [1]
ORDER_PROFILES = ["standard", "room_first"]


def _dec(v: Any) -> Decimal:
    if isinstance(v, (bool, float)):
        raise ValueError("floats are not allowed; use strings")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("not a decimal") from exc
    if not d.is_finite():
        raise ValueError("not finite")
    return d


def _money(v: Any) -> Decimal:
    d = _dec(v)
    if d < 0:
        raise ValueError("must be >= 0")
    if d != d.quantize(Decimal("0.01")):
        raise ValueError("more than 2 decimal places")
    return d.quantize(Decimal("0.01"))


def _percent(v: Any) -> Decimal:
    d = _dec(v)
    if d < 0 or d > 100:
        raise ValueError("percent must be within 0..100")
    return d


Money = Annotated[Decimal, BeforeValidator(_money)]
Percent = Annotated[Decimal, BeforeValidator(_percent)]
PosPercent = Annotated[Decimal, BeforeValidator(_dec)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoomRent(_Strict):
    type: Literal["percent_of_si"] = "percent_of_si"
    percent: PosPercent
    icu_percent: PosPercent
    per_day_cap: Money | None = None
    tier_caps: dict[str, Money] = Field(default_factory=dict)
    use_tier_caps: bool = False

    @field_validator("percent", "icu_percent")
    @classmethod
    def _nonneg(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("must be >= 0")
        return v


class CoPayConditions(_Strict):
    age_gte: int | None = Field(default=None, ge=0, le=130)


class CoPay(_Strict):
    percent: Percent = Decimal("0")
    conditions: CoPayConditions = CoPayConditions()


class Deductible(_Strict):
    amount: Money = Decimal("0.00")
    type: Literal["per_claim"] = "per_claim"  # per_year needs claim history: out of scope (DEDUCTIBLE_TYPE_UNSUPPORTED)


class Windows(_Strict):
    pre_days: int = Field(default=30, ge=0)
    post_days: int = Field(default=60, ge=0)


class Waiting(_Strict):
    initial: int = Field(ge=0)
    pre_existing: int = Field(ge=0)
    specific: dict[str, int] = Field(default_factory=dict)


class Exclusions(_Strict):
    icd_prefixes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    non_medical_policy: Literal["exclude_all", "exclude_listed", "allow"] = "exclude_all"


DEFAULT_PROP_APPLIES = [
    "doctor_fees", "surgeon_fees", "anaesthesia", "ot_charges", "nursing", "investigation", "procedure_package",
]
DEFAULT_PROP_EXEMPT = ["medicine", "implant", "consumable"]


class PolicyRules(_Strict):
    schema_version: Literal[1] = 1
    order_profile: Literal["standard", "room_first"] = "standard"
    room_rent: RoomRent  # required
    proportionate_deduction: bool = True
    proportionate_applies_to: list[str] = Field(default_factory=lambda: list(DEFAULT_PROP_APPLIES))
    proportionate_exempt: list[str] = Field(default_factory=lambda: list(DEFAULT_PROP_EXEMPT))
    sub_limits: dict[str, Money] = Field(default_factory=dict)
    line_caps: dict[str, Money] = Field(default_factory=dict)
    hospitalisation_windows: Windows = Windows()
    co_pay: CoPay = CoPay()
    non_network_co_pay_percent: Percent = Decimal("0")
    stack_co_pay: bool = False
    co_pay_order: Literal["after_deductible", "before_deductible"] = "after_deductible"
    deductible: Deductible = Deductible()
    waiting_periods_days: Waiting  # required
    pre_existing_group_map: dict[str, list[str]] = Field(default_factory=dict)
    accident_icd_prefixes: list[str] = Field(default_factory=lambda: ["S", "T"])
    exclusions: Exclusions  # required
    day_care_groups: list[str] = Field(default_factory=list)
    sum_insured_basis: Literal["individual", "floater"] = "individual"

    @model_validator(mode="after")
    def _overlap(self) -> PolicyRules:
        both = set(self.proportionate_applies_to) & set(self.proportionate_exempt)
        if both:
            raise ValueError(f"groups both applied and exempt: {sorted(both)}")
        return self


def validate_rules(raw: dict[str, Any]) -> PolicyRules:
    """Raise ``RulesInvalidError`` (API: 422 ``rules_invalid``) for any schema problem."""
    from pydantic import ValidationError

    from .errors import RulesInvalidError

    try:
        return PolicyRules.model_validate(raw)
    except ValidationError as exc:
        raise RulesInvalidError(str(exc)) from exc
