from __future__ import annotations

from typing import Any, Literal

from claim_contract.enums import DocType
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocTypePatch(Strict):
    doc_type: DocType


class QualityIn(Strict):
    quality_score: float = Field(ge=0, le=1)
    flags: list[str] = Field(default_factory=list, max_length=20)
    has_required_stamp: bool | None = None


class ParseIn(Strict):
    pass_no: int = Field(ge=1, le=3)
    engine: str = Field(min_length=1, max_length=64)
    engine_version: str | None = Field(default=None, max_length=32)
    typed_json: dict[str, Any]
    confidence: float = Field(ge=0, le=1)
    masked_text_key: str | None = Field(default=None, max_length=300)
    raw_markdown_key: str | None = Field(default=None, max_length=300)
    entities: dict[str, Any] | list[Any] | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class ClassifyIn(Strict):
    doc_type: DocType | None
    confidence: float = Field(ge=0, le=1)
    source: Literal["auto"] = "auto"


class StatusIn(Strict):
    parse_status: Literal["processing", "failed", "needs_review"]
    error: str | None = Field(default=None, max_length=500)


class SplitChild(Strict):
    doc_type_hint: DocType | None = None
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)


class SplitIn(Strict):
    children: list[SplitChild] = Field(min_length=2, max_length=20)
