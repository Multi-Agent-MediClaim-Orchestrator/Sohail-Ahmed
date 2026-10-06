"""Insurer -> hospital callbacks. Authentication is the HMAC ContractMiddleware (contract 01-01 §3); the
`insurer_hmac` guard only asserts that the middleware actually verified the request."""

from __future__ import annotations

import time
from typing import Any

from claim_contract import models as cm
from claim_contract.errors import from_validation_error
from claim_contract.middleware import key_id_var
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from app.auth.deps import guard
from app.core.deps import get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import callbacks as svc

router = APIRouter(prefix="/v1/insurer-callbacks", tags=["insurer-callbacks"])


@guard
async def insurer_hmac(request: Request) -> str:
    kid = key_id_var.get()
    if kid is None or kid != request.app.state.settings.insurer_key_id:
        raise ApiError("invalid_signature", "signature verification failed")
    return kid


async def _parse(request: Request, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as e:
        err = from_validation_error(e)
        raise ApiError(
            err.code, "request failed validation", errors=[x.model_dump() for x in err.errors]
        ) from None


class _Decision(BaseModel):
    claim_ref: str
    sequence: int
    decision: cm.Decision


class _Query(BaseModel):
    claim_ref: str
    sequence: int
    query: cm.Query


class _Settlement(BaseModel):
    claim_ref: str
    sequence: int
    settlement: cm.SettlementNotice


def _reply(status: int, body: Any, request: Request) -> Response:
    if status == 204 or body is None:
        resp = Response(status_code=204)
    else:
        resp = JSONResponse(body, status_code=status)
    if getattr(request.state, "replayed", False):
        resp.headers["Idempotent-Replay"] = "true"
    return resp


def _idem(request: Request) -> str:
    return request.headers.get("x-idempotency-key", "")


@router.post("/status", operation_id="callbackStatus", status_code=204)
async def status(
    request: Request, _: str = Depends(insurer_hmac), uow: UoW = Depends(get_uow)
) -> Response:
    upd = await _parse(request, cm.StatusUpdate)
    st, body = await svc.handle_status(uow, upd, _idem(request), request.app.state.hub)
    await uow.commit()
    return _reply(st, body, request)


@router.post("/decisions", operation_id="callbackDecision", status_code=204)
async def decisions(
    request: Request, _: str = Depends(insurer_hmac), uow: UoW = Depends(get_uow)
) -> Response:
    b = await _parse(request, _Decision)
    raw = b.model_dump(mode="json")
    st, body = await svc.handle_decision(
        uow, b.claim_ref, b.sequence, b.decision, _idem(request), raw, request.app.state.hub
    )
    await uow.commit()
    return _reply(st, body, request)


@router.post("/settlements", operation_id="callbackSettlement", status_code=204)
async def settlements(
    request: Request, _: str = Depends(insurer_hmac), uow: UoW = Depends(get_uow)
) -> Response:
    b = await _parse(request, _Settlement)
    raw = b.model_dump(mode="json")
    st, body = await svc.handle_settlement(
        uow, b.claim_ref, b.sequence, b.settlement, _idem(request), raw, request.app.state.hub
    )
    await uow.commit()
    return _reply(st, body, request)


@router.post("/queries", operation_id="callbackQuery", status_code=204)
async def queries(
    request: Request, _: str = Depends(insurer_hmac), uow: UoW = Depends(get_uow)
) -> Response:
    import json as _json

    from app.services import queries as qsvc

    try:
        rnd = (_json.loads(await request.body()).get("query") or {}).get("round")
    except ValueError:
        rnd = None
    if isinstance(rnd, int) and rnd > 3:
        ref = _json.loads(await request.body()).get("claim_ref", "")
        await qsvc.record_anomaly(uow, ref, {"reason": "round_exceeds_max", "round": rnd})
        await uow.commit()
        raise ApiError(
            "max_rounds_exceeded", "the maximum of 3 query rounds was exceeded", status=422
        )
    b = await _parse(request, _Query)
    raw = b.model_dump(mode="json")
    st = request.app.state
    code, body = await qsvc.handle_query(
        uow, b.claim_ref, b.sequence, b.query, _idem(request), raw, st.hub, st.n8n
    )
    await uow.commit()
    return _reply(code, body, request)


@router.post("/documents/{doc_id}/refresh-url", operation_id="refreshDocumentUrl")
async def refresh_url(
    doc_id: str, request: Request, _: str = Depends(insurer_hmac), uow: UoW = Depends(get_uow)
) -> Any:
    from datetime import UTC, datetime, timedelta

    req = await _parse(request, cm.DocRefreshRequest)
    st = request.app.state
    case = (
        await uow.session.execute(
            text("SELECT id FROM claim_case WHERE claim_ref=:r"), {"r": req.claim_ref}
        )
    ).first()
    if case is None:
        raise ApiError("unknown_claim", f"unknown claim {req.claim_ref}")
    try:
        import uuid

        did = uuid.UUID(doc_id)
    except ValueError:
        raise ApiError("unknown_document", "unknown document") from None
    doc = (
        await uow.session.execute(
            text(
                "SELECT id, case_id, sha256, size_bytes, storage_key, lifecycle, scan_status FROM document WHERE id=:i"
            ),
            {"i": did},
        )
    ).first()
    if (
        doc is None
        or doc.lifecycle != "active"
        or doc.scan_status != "clean"
        or not doc.storage_key
    ):
        raise ApiError("unknown_document", "unknown document")
    if doc.case_id != case.id:
        raise ApiError("not_your_claim", "the document does not belong to that claim")
    key = f"rl:hosp:refresh:{did}:{int(time.time() // 3600)}"
    n = await st.redis.incr(key)
    if n == 1:
        await st.redis.expire(key, 7200)
    if n > 10:
        raise ApiError(
            "rate_limited",
            "at most 10 URL refreshes per document per hour",
            headers={"Retry-After": "3600"},
        )
    url = await st.store.presign_get(doc.storage_key, 3600)
    out = cm.DocRefreshResponse(
        doc_id=did,
        download_url=url,
        expires_at=datetime.now(UTC) + timedelta(hours=1),  # type: ignore[arg-type]
        sha256=doc.sha256,
        size_bytes=doc.size_bytes,
    )
    return out.model_dump(mode="json")
