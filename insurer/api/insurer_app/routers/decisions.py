"""Decision gate endpoints (03-04 §4.1)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict

from ..security.auth import Principal, require_internal, require_roles
from ..services import decision
from ..services.decision import SubmitBody, VoteBody

router = APIRouter(tags=["decisions"])
reviewer_up = require_roles("reviewer", "senior_reviewer", "approver", "admin")


@router.get("/v1/cases/{case_id}/decision")
async def get_decision(case_id: UUID, request: Request, p: Principal = Depends(reviewer_up)) -> dict[str, Any]:
    return await decision.preview(request.app.state.config, case_id)


@router.post("/v1/cases/{case_id}/decision/recommend")
async def recommend(case_id: UUID, request: Request, p: Principal = Depends(require_roles("reviewer", "senior_reviewer", "admin", "n8n-service"))) -> dict[str, Any]:
    """Return the current recommendation (a fresh one comes from a verification run: POST /verification/rerun)."""
    return await decision.preview(request.app.state.config, case_id)


@router.post("/v1/cases/{case_id}/decision/submit")
async def submit(case_id: UUID, body: SubmitBody, request: Request, if_match: str | None = Header(default=None),
                 p: Principal = Depends(require_roles("reviewer", "senior_reviewer"))) -> dict[str, Any]:
    return await decision.submit_decision(request.app.state.config, case_id, p, body, if_match)


@router.post("/v1/decisions/{decision_id}/approvals")
async def vote(decision_id: UUID, body: VoteBody, request: Request, p: Principal = Depends(require_roles("approver", "senior_reviewer"))) -> dict[str, Any]:
    return await decision.record_vote(request.app.state.config, decision_id, p, body)


@router.get("/v1/approvals/queue")
async def queue(p: Principal = Depends(require_roles("approver", "senior_reviewer"))) -> dict[str, Any]:
    return {"items": await decision.approval_queue(p)}


class WithdrawBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/v1/cases/{case_id}/decision/withdraw-task")
async def withdraw_task(case_id: UUID, body: WithdrawBody, p: Principal = Depends(require_roles("senior_reviewer"))) -> dict[str, Any]:
    return await decision.withdraw_task(case_id, p, body.reason)


@router.get("/v1/admin/gate/stats")
async def gate_stats(p: Principal = Depends(require_roles("admin"))) -> dict[str, Any]:
    return await decision.gate_stats()


@router.post("/internal/cases/{case_id}/decision/finalize-callback")
async def finalize_callback(case_id: UUID, p: Principal = Depends(require_internal)) -> dict[str, Any]:
    return {"accepted": True}  # idempotent notification from n8n; outbound callbacks are already queued transactionally
