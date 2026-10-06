from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.auth.deps import require_access, require_role
from app.auth.principal import Principal
from app.completeness import service as comp
from app.core.deps import get_uow
from app.core.uow import UoW
from app.services.cases import load_case

router = APIRouter(tags=["completeness"])
Staff = Depends(require_role("desk", "officer"))
Officer = Depends(require_role("officer"))
Reader = Depends(require_role("desk", "officer", "admin"))
Internal = Depends(require_access(services=("svc-n8n", "svc-internal")))


class WaiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.get("/v1/cases/{case_id}/completeness", operation_id="getCompleteness")
async def get_completeness(
    case_id: str,
    request: Request,
    history: bool = False,
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    from app.core.errors import ApiError

    try:
        out = await comp.latest(uow, case.id, history)
    except ApiError as e:
        if e.code != "not_found":
            raise
        await comp.run(
            uow, case.id, "manual", hub=request.app.state.hub, settings=request.app.state.settings
        )
        out = await comp.latest(uow, case.id, history)
    return out


@router.post("/v1/cases/{case_id}/completeness/run", operation_id="runCompleteness")
async def run_completeness(
    case_id: str, request: Request, p: Principal = Staff, uow: UoW = Depends(get_uow)
) -> Any:
    case = await load_case(uow, p, case_id)
    await comp.run(
        uow, case.id, "manual", hub=request.app.state.hub, settings=request.app.state.settings
    )
    return await comp.latest(uow, case.id)


@router.post("/v1/cases/{case_id}/requirements/{doc_type}/waive", operation_id="waiveRequirement")
async def waive(
    case_id: str,
    doc_type: str,
    body: WaiveBody,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    return await comp.waive(
        uow, p, case, doc_type, body.reason, request.app.state.hub, request.app.state.settings
    )


@router.delete("/v1/cases/{case_id}/requirements/{doc_type}/waive", operation_id="revokeWaiver")
async def revoke(
    case_id: str,
    doc_type: str,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    return await comp.revoke_waiver(
        uow, p, case, doc_type, request.app.state.hub, request.app.state.settings
    )


@router.get("/v1/cases/{case_id}/doc-requests", operation_id="listDocRequests")
async def doc_requests(case_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    case = await load_case(uow, p, case_id)
    return {"items": await comp.open_requests(uow, case.id)}


@router.post(
    "/v1/cases/{case_id}/doc-requests/{request_id}/remind", operation_id="remindDocRequest"
)
async def remind(
    case_id: str,
    request_id: str,
    request: Request,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    return await comp.remind(uow, request.app.state.redis, case.id, request_id, p)


@router.post(
    "/v1/internal/cases/{case_id}/completeness/run", operation_id="internalRunCompleteness"
)
async def internal_run(
    case_id: str, request: Request, _: Principal = Internal, uow: UoW = Depends(get_uow)
) -> Any:
    out = await comp.run(
        uow, case_id, "doc_event", hub=request.app.state.hub, settings=request.app.state.settings
    )
    return out
