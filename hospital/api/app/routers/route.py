from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.auth.deps import require_access, require_role
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.uow import UoW
from app.router_engine import service as svc
from app.services.cases import load_case

router = APIRouter(tags=["route"])
Reader = Depends(require_role("desk", "officer", "admin"))
Officer = Depends(require_role("officer"))
Internal = Depends(require_access(services=("svc-n8n", "svc-crew", "svc-internal")))


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_type: str | None = None
    admission_type: str | None = None
    flags_add: list[str] | None = None
    flags_remove: list[str] | None = None
    reason: str


class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codes: list[str] | None = None


class ConvertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str
    reason: str


@router.get("/v1/cases/{case_id}/route", operation_id="getRoute")
async def get_route(case_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    case = await load_case(uow, p, case_id)
    return await svc.get_route(uow, case.id)


@router.post("/v1/cases/{case_id}/route/recompute", operation_id="recomputeRoute")
async def recompute(
    case_id: str,
    request: Request,
    clear_overrides: bool = False,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    st = request.app.state
    out = await svc.recompute(
        uow,
        case.id,
        "manual",
        p.actor_id,
        hub=st.hub,
        completeness=st.completeness,
        clear_overrides=clear_overrides,
    )
    if not out["changed"]:
        return {"changed": False, "seq": out["seq"]}
    return out


@router.post("/v1/cases/{case_id}/route/override", operation_id="overrideRoute")
async def override(
    case_id: str,
    body: OverrideBody,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    st = request.app.state
    return await svc.override(
        uow, p, case.id, body.model_dump(exclude_unset=True), st.hub, st.completeness
    )


@router.post("/v1/cases/{case_id}/route/ack", operation_id="ackRoute")
async def ack(
    case_id: str, body: AckBody, p: Principal = Officer, uow: UoW = Depends(get_uow)
) -> Any:
    case = await load_case(uow, p, case_id)
    return await svc.acknowledge(uow, p, case.id, body.codes)


@router.post("/v1/cases/{case_id}/convert", operation_id="convertCase")
async def convert(
    case_id: str,
    body: ConvertBody,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await load_case(uow, p, case_id)
    st = request.app.state
    return await svc.convert(
        uow, p, case.id, body.to, body.reason, st.store, st.hub, st.completeness
    )


@router.post("/v1/internal/cases/{case_id}/route/recompute", operation_id="internalRecomputeRoute")
async def internal_recompute(
    case_id: str, request: Request, _: Principal = Internal, uow: UoW = Depends(get_uow)
) -> Any:
    st = request.app.state
    return await svc.recompute(
        uow, case_id, "manual", "svc", hub=st.hub, completeness=st.completeness
    )
