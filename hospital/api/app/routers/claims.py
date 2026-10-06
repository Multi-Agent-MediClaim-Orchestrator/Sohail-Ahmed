from __future__ import annotations

from typing import Any

from claim_contract.enums import WithdrawReason
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.deps import require_access, require_role
from app.auth.principal import Principal
from app.claim_validation.schemas import DraftResult
from app.core.deps import get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import claim_builder, submission
from app.services.cases import load_case
from app.services.claim_edit import PatchOp

router = APIRouter(tags=["claims"])
Staff = Depends(require_role("desk", "officer"))
Officer = Depends(require_role("officer"))
Reader = Depends(require_role("desk", "officer", "admin"))
Admin = Depends(require_role("admin"))
InternalCrew = Depends(require_access(services=("svc-crew",)))
InternalJobs = Depends(require_access(services=("svc-n8n", "svc-internal")))


class SignoffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approved|returned)$")
    comment: str | None = Field(default=None, max_length=1000)
    acknowledged_warnings: list[str] = Field(default_factory=list, max_length=50)


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    late_filing_reason: str | None = Field(default=None, max_length=1500)


class WithdrawBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: WithdrawReason
    note: str | None = Field(default=None, max_length=1000)


async def _case(uow: UoW, p: Principal, case_id: str) -> Any:
    c = await load_case(uow, p, case_id)
    return await claim_builder.load_case_row(uow, c.id)


@router.post("/v1/cases/{case_id}/claim/build", status_code=202, operation_id="buildClaim")
async def build(
    case_id: str, request: Request, p: Principal = Staff, uow: UoW = Depends(get_uow)
) -> Any:
    case = await load_case(uow, p, case_id)
    st = request.app.state
    return await claim_builder.start(
        uow, p, case.id, st.crew, st.redis, st.hub, st.settings, st.completeness
    )


@router.get("/v1/cases/{case_id}/claim", operation_id="getClaim")
async def get_claim(case_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    return await submission.get_claim(uow, await _case(uow, p, case_id))


@router.get("/v1/cases/{case_id}/claim/versions", operation_id="claimVersions")
async def versions(case_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    return await submission.versions(uow, (await _case(uow, p, case_id)).id)


@router.put("/v1/cases/{case_id}/claim", operation_id="editClaim")
async def edit(
    case_id: str,
    ops: list[PatchOp],
    request: Request,
    if_match: str | None = Header(None),
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await _case(uow, p, case_id)
    return await submission.edit(uow, p, case.id, ops, if_match, request.app.state.hub)


@router.post("/v1/cases/{case_id}/claim/validate", operation_id="validateClaim")
async def validate_claim(case_id: str, p: Principal = Staff, uow: UoW = Depends(get_uow)) -> Any:
    return await submission.revalidate(uow, await _case(uow, p, case_id))


@router.post("/v1/cases/{case_id}/claim/signoff", operation_id="signoffClaim")
async def signoff(
    case_id: str,
    body: SignoffBody,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await _case(uow, p, case_id)
    st = request.app.state
    return await submission.signoff(
        uow,
        p,
        case.id,
        body.decision,
        body.comment,
        body.acknowledged_warnings,
        st.hub,
        st.completeness,
    )


@router.post("/v1/cases/{case_id}/claim/submit", status_code=202, operation_id="submitClaim")
async def submit(
    case_id: str,
    request: Request,
    body: SubmitBody | None = None,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await _case(uow, p, case_id)
    st = request.app.state
    out = await submission.submit(
        uow, p, case.id, (body or SubmitBody()).late_filing_reason, st.store, st.settings, st.hub
    )
    worker = getattr(st, "outbox", None)
    if worker is not None:
        worker.wake()
    return out


@router.get("/v1/cases/{case_id}/submission", operation_id="getSubmission")
async def get_submission(case_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    return await submission.submission_status(uow, await _case(uow, p, case_id))


@router.post("/v1/cases/{case_id}/submission/retry", operation_id="retrySubmission")
async def retry(
    case_id: str, request: Request, p: Principal = Officer, uow: UoW = Depends(get_uow)
) -> Any:
    case = await _case(uow, p, case_id)
    out = await submission.retry(uow, p, case.id)
    if getattr(request.app.state, "outbox", None) is not None:
        request.app.state.outbox.wake()
    return out


@router.post("/v1/cases/{case_id}/submission/reopen", operation_id="reopenSubmission")
async def reopen(
    case_id: str, request: Request, p: Principal = Officer, uow: UoW = Depends(get_uow)
) -> Any:
    case = await _case(uow, p, case_id)
    return await submission.reopen(uow, p, case.id, request.app.state.hub)


@router.post("/v1/cases/{case_id}/claim/withdraw", operation_id="withdrawClaim")
async def withdraw(
    case_id: str,
    body: WithdrawBody,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    case = await _case(uow, p, case_id)
    out = await submission.withdraw(uow, p, case.id, body.reason.value, body.note)
    if getattr(request.app.state, "outbox", None) is not None:
        request.app.state.outbox.wake()
    return out


@router.get("/v1/admin/outbox", operation_id="adminOutbox")
async def admin_outbox(
    status: str = "dead", limit: int = 100, _: Principal = Admin, uow: UoW = Depends(get_uow)
) -> Any:
    from sqlalchemy import text

    if status not in ("pending", "sending", "sent", "failed", "dead"):
        raise ApiError("validation_error", "unknown outbox status")
    rows = (
        await uow.session.execute(
            text(
                "SELECT o.id, o.kind, o.status, o.attempts, o.last_error, o.created_at, c.claim_ref FROM outbox o JOIN claim_case c ON c.id=o.case_id "
                "WHERE o.status=:s ORDER BY o.created_at DESC LIMIT :l"
            ),
            {"s": status, "l": min(max(limit, 1), 500)},
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "claim_ref": r.claim_ref,
                "kind": r.kind,
                "status": r.status,
                "attempts": r.attempts,
                "last_error": r.last_error,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/v1/internal/cases/{case_id}/claim/draft", operation_id="cbDraft")
async def crew_draft(
    case_id: str,
    body: DraftResult,
    request: Request,
    p: Principal = InternalCrew,
    uow: UoW = Depends(get_uow),
) -> Any:
    st = request.app.state
    return await claim_builder.accept_draft(
        uow,
        p,
        case_id,
        body,
        crew=st.crew,
        redis=st.redis,
        hub=st.hub,
        completeness=st.completeness,
        settings=st.settings,
    )


@router.post("/v1/internal/jobs/claim-build-timeouts", operation_id="jobClaimBuildTimeouts")
async def timeouts(
    request: Request, _: Principal = InternalJobs, uow: UoW = Depends(get_uow)
) -> Any:
    st = request.app.state
    return await claim_builder.expire_builds(uow, st.redis, st.hub, st.completeness)
