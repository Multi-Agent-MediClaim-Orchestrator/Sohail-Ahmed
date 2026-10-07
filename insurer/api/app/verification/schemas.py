"""Finding + step result schemas (03-03 §3, §4.3)."""

from __future__ import annotations

import hashlib
from typing import Any

from claim_contract.enums import DocType, QueryCategory, Severity, StepName
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .codes import known


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str | None = None
    page: int | None = None
    field: str | None = None
    value_masked: str | None = None
    bbox: list[float] | None = None  # normalised 0..1 [x0,y0,x1,y1], origin top-left


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Severity
    message: str = Field(max_length=500)
    evidence: list[Evidence] = Field(default_factory=list)
    fixable: bool = False
    suggested_query_category: QueryCategory | None = None
    suggested_doc_types: list[DocType] = Field(default_factory=list)
    detail: str | None = None  # extra discriminator, e.g. a diagnosis code or group name
    key: str | None = None  # server-assigned finding key
    overridden: bool = False

    @model_validator(mode="after")
    def _known(self) -> Finding:
        if not known(self.code):
            raise ValueError(f"unknown finding code {self.code!r}")
        return self


def finding_key(step: str, code: str, evidence_ref: str = "") -> str:
    """sha256(step|code|evidence_ref): an override only applies while the evidence stays the same (03 §8.1)."""
    return hashlib.sha256(f"{step}|{code}|{evidence_ref}".encode()).hexdigest()


def evidence_ref(f: Finding) -> str:
    parts = sorted(f"{e.doc_id or ''}:{e.page or ''}:{e.field or ''}" for e in f.evidence)
    return "|".join(parts) + (f"#{f.detail}" if f.detail else "") + "|" + ",".join(sorted(d.value for d in f.suggested_doc_types))


class AgentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str = "0"
    prompt_version: str = ""
    trace_id: str | None = None


class StepResultBody(BaseModel):
    """Body posted by n8n for agent-backed steps. For pure-rule steps the API computes everything itself."""

    model_config = ConfigDict(extra="forbid")
    step: StepName
    agent: AgentInfo | None = None
    deterministic: dict[str, Any] | None = None
    score: float | None = None
    findings: list[Finding] = Field(default_factory=list)
    agent_notes: str | None = None
    degraded: bool = False
    agent_output: dict[str, Any] | None = None
    line_mapping: dict[str, dict[str, Any]] | None = None  # calculation step: line_ref -> {mapped_group, procedure_group, tags, ...}
    crew_attempts: int = 1
    failure: str | None = None  # 'agent_unavailable' | 'agent_invalid_output' (agent could not deliver)


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: str = "initial"
    steps: list[str] = Field(default_factory=lambda: ["all"])
    client_token: str | None = None
    force: bool = False


STEP_ORDER = ["document_fetch", "completeness", "identity", "authenticity", "coverage", "calculation"]
AGENT_STEPS = {"identity", "authenticity", "coverage", "calculation"}  # calculation: mapping only; maths is the engine's
PREREQ = {"identity": ["completeness"], "authenticity": ["completeness"], "coverage": ["identity"], "calculation": ["coverage"]}
