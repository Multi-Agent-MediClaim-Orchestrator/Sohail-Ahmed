from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.auth.deps import require_role
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.uow import UoW
from app.schemas.cases import AssignBody, CaseCreate, CasePatch, TransitionBody
from app.services import cases as svc
from app.services import transitions
from app.services.cases import load_case

router = APIRouter(prefix="/v1/cases", tags=["cases"])
Staff = Depends(require_role("desk", "officer"))
Reader = Depends(require_role("desk", "officer", "admin"))


@router.post("", status_code=201, operation_id="createCase")
async def create_case(
    body: CaseCreate,
    request: Request,
    response: Response,
    confirm_patient_update: bool = False,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    out = await svc.create_case(
        uow, p, body, request.app.state.settings, request.app.state.hub, confirm_patient_update
    )
    response.headers["Location"] = f"/v1/cases/{out['id']}"
    response.headers["ETag"] = f'"{out["version"]}"'
    return out


@router.get("", operation_id="listCases")
async def list_cases(
    status: list[str] | None = Query(None),
    q: str | None = None,
    assigned: str | None = None,
    claim_type: str | None = None,
    flag: str | None = None,
    cursor: str | None = None,
    size: int = Query(25, ge=1, le=100),
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    return await svc.list_cases(
        uow,
        p,
        statuses=status,
        q=q,
        assigned=assigned,
        claim_type=claim_type,
        flag=flag,
        cursor=cursor,
        size=size,
    )


@router.get("/{case_id}", operation_id="getCase")
async def get_case(
    case_id: str,
    request: Request,
    response: Response,
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    out = await svc.get_case(uow, p, case_id, request.app.state.settings)
    response.headers["ETag"] = f'"{out["version"]}"'
    return out


@router.patch("/{case_id}", operation_id="patchCase")
async def patch_case(
    case_id: str,
    body: CasePatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None),
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    out = await svc.patch_case(uow, p, case_id, body, if_match, request.app.state.settings)
    if svc.ROUTE_FIELDS & set(body.model_dump(exclude_unset=True)):
        from app.router_engine import service as router_svc  # noqa: PLC0415

        st = request.app.state
        await router_svc.recompute(
            uow, out["id"], "patch", p.actor_id, hub=st.hub, completeness=st.completeness
        )
        out = await svc.get_case(uow, p, out["id"], st.settings)
    response.headers["ETag"] = f'"{out["version"]}"'
    return out


@router.post("/{case_id}/assign", operation_id="assignCase")
async def assign(
    case_id: str,
    body: AssignBody,
    p: Principal = Depends(require_role("officer", "admin")),
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    return await svc.assign_case(uow, p, case_id, body.user_id)


@router.post("/{case_id}/transition", operation_id="transitionCase")
async def transition(
    case_id: str,
    body: TransitionBody,
    request: Request,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    c = await load_case(uow, p, case_id)
    out = await transitions.transition(
        uow, c.id, body.to, p, reason=body.reason, manual=True, hub=request.app.state.hub
    )
    await uow.commit()
    if body.to in ("docs_complete", "docs_pending"):
        await request.app.state.completeness(str(c.id), "manual")
    return out


@router.get("/{case_id}/timeline", operation_id="caseTimeline")
async def timeline(
    case_id: str,
    cursor: str | None = None,
    size: int = Query(50, ge=1, le=200),
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    return await svc.timeline(uow, p, case_id, cursor, size)
