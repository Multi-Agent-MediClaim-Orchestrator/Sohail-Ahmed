"""Hospital -> insurer endpoints (contract 6.1). HMAC, idempotency, rate limit and body cap are enforced by
``ContractAuthMiddleware`` before these handlers run; the raw body is available at ``request.state.raw_body``."""

from __future__ import annotations

from typing import Any

from claim_contract.errors import FieldError, ProblemError
from claim_contract.models import DocSupplement, WithdrawRequest
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..db import sessionmaker
from ..services import claims, receipt
from ..settings import get_settings

router = APIRouter(prefix="/v1/hospital-api", tags=["hospital-api"])


def _key_id(request: Request) -> str:
    return request.state.contract_auth.key_id  # type: ignore[no-any-return]


def _parse(model: Any, request: Request) -> Any:
    try:
        return model.model_validate_json(request.state.raw_body)
    except ValidationError as exc:
        errs = [FieldError(field=".".join(str(p) for p in e["loc"]), message=e["msg"]) for e in exc.errors()]
        raise ProblemError("validation_error", "request body failed validation", errors=errs) from exc


@router.post("/claims", status_code=202)
async def submit_claim(request: Request) -> JSONResponse:
    ack, replayed = await receipt.receive_claim(request.state.raw_body, _key_id(request), request.app.state.config, get_settings())
    return JSONResponse(ack.model_dump(mode="json"), status_code=200 if replayed else 202, headers={"Idempotent-Replay": "true"} if replayed else {})


@router.get("/claims/{claim_ref}")
async def claim_status(claim_ref: str, request: Request, include: str | None = Query(default=None)) -> dict[str, Any]:
    async with sessionmaker()() as s:
        h = await claims.hospital_for_key(s, _key_id(request))
        return await claims.status_for(s, h, claim_ref, include_queries=(include == "queries"))


@router.post("/claims/{claim_ref}/documents", status_code=202)
async def supplement_documents(claim_ref: str, request: Request) -> dict[str, Any]:
    sup = _parse(DocSupplement, request)
    async with sessionmaker()() as s:
        h = await claims.hospital_for_key(s, _key_id(request))
    return await claims.supplement(sup, h, claim_ref)


@router.get("/claims/{claim_ref}/queries")
async def claim_queries(claim_ref: str, request: Request, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None) -> dict[str, Any]:
    async with sessionmaker()() as s:
        h = await claims.hospital_for_key(s, _key_id(request))
        return await claims.list_queries(s, h, claim_ref, limit, cursor)


@router.post("/claims/{claim_ref}/withdraw")
async def withdraw_claim(claim_ref: str, request: Request) -> dict[str, Any]:
    req = _parse(WithdrawRequest, request)
    async with sessionmaker()() as s:
        h = await claims.hospital_for_key(s, _key_id(request))
    return await claims.withdraw(req, h, claim_ref)
