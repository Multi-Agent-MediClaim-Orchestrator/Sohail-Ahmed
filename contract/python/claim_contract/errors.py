"""RFC 7807 problem details and exceptions (01-01 section 8)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

TYPE_BASE = "https://claims.local/errors/"

# code -> (http status, retryable, title)
CATALOGUE: dict[str, tuple[int, bool, str]] = {
    "unsupported_version": (400, False, "Unsupported contract version"),
    "bad_request": (400, False, "Bad request"),
    "invalid_signature": (401, False, "Invalid signature"),
    "stale_request": (401, True, "Stale request"),
    "forbidden": (403, False, "Forbidden"),
    "not_your_claim": (403, False, "Not your claim"),
    "hospital_blacklisted": (403, False, "Hospital not permitted"),
    "hospital_mismatch": (403, False, "Hospital mismatch"),
    "unknown_claim": (404, False, "Unknown claim"),
    "unknown_query": (404, False, "Unknown query"),
    "unknown_document": (404, False, "Unknown document"),
    "duplicate_claim": (409, False, "Duplicate claim"),
    "idempotency_conflict": (409, False, "Idempotency conflict"),
    "idempotency_in_progress": (409, True, "Idempotency in progress"),
    "invalid_transition": (409, False, "Invalid transition"),
    "payload_too_large": (413, False, "Payload too large"),
    "validation_error": (422, False, "Validation error"),
    "totals_mismatch": (422, False, "Totals mismatch"),
    "rate_limited": (429, True, "Rate limited"),
    "internal_error": (500, True, "Internal error"),
    "doc_unavailable": (424, True, "Document unavailable"),
    "service_unavailable": (503, True, "Service unavailable"),
}


class FieldError(BaseModel):
    field: str
    message: str


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    errors: list[FieldError] = []
    trace_id: str | None = None
    retryable: bool = False


class ContractError(Exception):
    def __init__(
        self,
        code: str,
        detail: str = "",
        errors: list[FieldError] | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.errors = errors or []
        self.trace_id = trace_id

    def problem(self) -> ProblemDetail:
        status, retryable, title = CATALOGUE[self.code]
        return ProblemDetail(
            type=TYPE_BASE + self.code,
            title=title,
            status=status,
            detail=self.detail or title,
            code=self.code,
            errors=self.errors,
            trace_id=self.trace_id,
            retryable=retryable,
        )


class InvalidSignature(ContractError):
    def __init__(self) -> None:
        super().__init__("invalid_signature", "signature verification failed")


class StaleRequest(ContractError):
    def __init__(self) -> None:
        super().__init__("stale_request", "timestamp outside allowed skew")


def from_validation_error(exc: Any, trace_id: str | None = None) -> ContractError:
    """Convert a pydantic ValidationError to a ContractError."""
    errs = [
        FieldError(field=".".join(str(p) for p in e["loc"]), message=str(e["msg"]))
        for e in exc.errors()
    ]
    code = (
        "totals_mismatch"
        if any("totals_mismatch" in e.message for e in errs)
        else "validation_error"
    )
    return ContractError(code, "request failed validation", errs, trace_id)
