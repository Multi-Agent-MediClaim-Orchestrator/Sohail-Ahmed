"""RFC 7807 problem details and exceptions (01-01 section 8)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

TYPE_BASE = "https://claims.local/errors/"
ERROR_BASE = TYPE_BASE

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
    # added for the insurer side (contract 1.1)
    "forbidden_role": (403, False, "Role not permitted"),
    "not_found": (404, False, "Not found"),
    "request_in_progress": (409, True, "Request in progress"),
    "query_closed": (409, False, "Query closed"),
    "stale_etag": (412, False, "Precondition failed"),
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
    def __init__(self, detail: str = "signature verification failed") -> None:
        super().__init__("invalid_signature", detail)


class StaleRequest(ContractError):
    def __init__(self) -> None:
        super().__init__("stale_request", "timestamp outside allowed skew")


SigningError = ContractError  # Dev B code catches this name


class ProblemError(ContractError):
    """Insurer-side exception: like ContractError but may override status/headers/title and accepts any code."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        status: int | None = None,
        errors: list[FieldError] | list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool | None = None,
        title: str | None = None,
    ) -> None:
        std = CATALOGUE.get(code, (500, False, code.replace("_", " ").title()))
        fe = [e if isinstance(e, FieldError) else FieldError(**e) for e in (errors or [])]
        super().__init__(code, detail or "", fe)
        self.status = status if status is not None else std[0]
        self.retryable = std[1] if retryable is None else retryable
        self.title = title or std[2]
        self.headers = headers or {}

    def problem(self) -> ProblemDetail:
        return ProblemDetail(
            type=ERROR_BASE + self.code,
            title=self.title,
            status=self.status,
            detail=self.detail or self.title,
            code=self.code,
            errors=self.errors,
            trace_id=self.trace_id,
            retryable=self.retryable,
        )

    def to_problem(self, trace_id: str | None = None) -> ProblemDetail:
        p = self.problem()
        p.trace_id = trace_id or p.trace_id or str(uuid.uuid4())
        return p


class InvalidTransition(ProblemError):
    def __init__(self, current: str, target: str, detail: str | None = None) -> None:
        super().__init__(
            "invalid_transition", detail or f"transition {current} -> {target} is not allowed"
        )
        self.current, self.target = current, target


def problem_response(
    exc: ProblemError, trace_id: str | None = None
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = exc.to_problem(trace_id).model_dump(mode="json")
    return exc.status, body, exc.headers


def install_handlers(app: Any) -> None:
    """Attach handlers to a FastAPI app so ProblemError / validation / HTTP errors give problem+json."""
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _trace(request: Request) -> str:
        return getattr(request.state, "request_id", None) or str(uuid.uuid4())

    def _resp(pe: ProblemError, request: Request) -> JSONResponse:
        status, body, headers = problem_response(pe, _trace(request))
        return JSONResponse(
            body, status_code=status, headers=headers, media_type="application/problem+json"
        )

    @app.exception_handler(ContractError)
    async def _problem(request: Request, exc: ContractError) -> JSONResponse:
        if not isinstance(exc, ProblemError):
            exc = ProblemError(exc.code, exc.detail, errors=exc.errors)
        return _resp(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _resp(
            ProblemError("validation_error", "request validation failed", errors=errs), request
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "bad_request",
            401: "invalid_signature",
            403: "forbidden",
        }.get(exc.status_code, "bad_request" if exc.status_code < 500 else "internal_error")
        return _resp(ProblemError(code, str(exc.detail), status=exc.status_code), request)

    @app.exception_handler(RecursionError)
    async def _too_deep(request: Request, exc: RecursionError) -> JSONResponse:
        return _resp(ProblemError("bad_request", "request body is nested too deeply"), request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return _resp(ProblemError("internal_error", "unexpected error"), request)


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
