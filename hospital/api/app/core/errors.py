"""RFC 7807 errors. Contract-level codes reuse claim_contract.errors; hospital-private codes are
registered here (doc 02 §8.2). Every error response carries a trace_id."""

from __future__ import annotations

from typing import Any

from claim_contract.errors import CATALOGUE as CONTRACT_CATALOGUE
from claim_contract.errors import ContractError, from_validation_error
from claim_contract.middleware import request_id_var
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

TYPE_BASE = "https://claims.local/errors/"

# code -> (status, title)
PRIVATE: dict[str, tuple[int, str]] = {
    "unauthenticated": (401, "Authentication required"),
    "invalid_token": (401, "Invalid token"),
    "jwks_unavailable": (401, "Identity provider unavailable"),
    "forbidden": (403, "Forbidden"),
    "no_role": (403, "No hospital role"),
    "user_disabled": (403, "User disabled"),
    "wrong_service": (403, "Wrong service identity"),
    "email_conflict": (409, "Email conflict"),
    "cannot_deactivate_self": (409, "Cannot deactivate yourself"),
    "last_admin": (409, "Cannot deactivate the last admin"),
    "not_found": (404, "Not found"),
    "patient_conflict": (409, "Patient details conflict"),
    "invalid_state": (409, "Invalid state"),
    "invalid_transition": (409, "Invalid transition"),
    "precondition_failed": (412, "Precondition failed"),
    "precondition_required": (428, "If-Match required"),
    "case_locked": (423, "Case locked"),
    "case_submitted": (409, "Case already submitted"),
    "duplicate_document": (409, "Duplicate document"),
    "document_not_active": (409, "Document not active"),
    "scan_unavailable": (503, "Virus scanner unavailable"),
    "unsupported_media_type": (422, "Unsupported media type"),
    "infected": (422, "Infected file"),
    "encrypted_pdf": (422, "Encrypted PDF"),
    "active_content": (422, "PDF contains active content"),
    "corrupt_file": (422, "Corrupt file"),
    "empty_file": (422, "Empty file"),
    "file_too_large": (413, "File too large"),
    "image_too_large": (422, "Image too large"),
    "too_many_files": (413, "Too many files"),
    "invalid_supersede": (422, "Invalid supersede"),
    "gone": (410, "Gone"),
    "rate_limited": (429, "Rate limited"),
    "validation_error": (422, "Validation error"),
    "bad_request": (400, "Bad request"),
    "override_conflicts_evidence": (409, "Override conflicts with evidence"),
    "already_converted": (409, "Case already converted"),
    "not_waivable": (409, "Requirement not waivable"),
    "reason_too_short": (422, "Reason too short"),
    "unknown_rule": (404, "Unknown rule"),
    "config_unavailable": (503, "Configuration unavailable"),
    "two_person_rule": (409, "Two-person rule"),
    "config_invalid": (422, "Configuration invalid"),
    "config_conflict": (409, "Configuration conflict"),
    "internal_error": (500, "Internal error"),
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        status: int | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        if code in PRIVATE:
            st, self.title = PRIVATE[code]
        elif code in CONTRACT_CATALOGUE:
            st, _, self.title = CONTRACT_CATALOGUE[code]
        else:
            raise KeyError(f"unregistered error code {code}")
        self.status, self.code, self.detail = status or st, code, detail or self.title
        self.errors, self.headers, self.extra = errors or [], headers or {}, extra


def Unauthenticated(code: str = "unauthenticated", detail: str = "") -> ApiError:  # noqa: N802
    return ApiError(code, detail, headers={"WWW-Authenticate": f'Bearer error="{code}"'})


def Forbidden(code: str = "forbidden", detail: str = "", **extra: Any) -> ApiError:  # noqa: N802
    return ApiError(code, detail, **extra)


def problem(err: ApiError) -> dict[str, Any]:
    body = {
        "type": TYPE_BASE + err.code,
        "title": err.title,
        "status": err.status,
        "code": err.code,
        "detail": err.detail,
        "errors": err.errors,
        "trace_id": request_id_var.get(),
    }
    body.update(err.extra)
    return body


def install_handlers(app: FastAPI) -> None:
    def respond(err: ApiError) -> JSONResponse:
        return JSONResponse(
            problem(err),
            status_code=err.status,
            headers=err.headers,
            media_type="application/problem+json",
        )

    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:
        return respond(exc)

    @app.exception_handler(ContractError)
    async def _contract(request: Request, exc: ContractError) -> JSONResponse:
        return respond(ApiError(exc.code, exc.detail, errors=[e.model_dump() for e in exc.errors]))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": str(e["msg"])}
            for e in exc.errors()
        ]
        return respond(ApiError("validation_error", "request failed validation", errors=errs))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "bad_request", 431: "bad_request"}.get(
            exc.status_code, "bad_request"
        )
        e = ApiError(code, str(exc.detail), status=exc.status_code)
        return respond(e)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("app.errors").exception("unhandled error")  # logs never include tokens
        return respond(ApiError("internal_error", "unexpected error"))


__all__ = [
    "ApiError",
    "Forbidden",
    "Unauthenticated",
    "from_validation_error",
    "install_handlers",
    "problem",
]
