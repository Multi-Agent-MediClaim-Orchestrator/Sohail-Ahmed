"""Query inbox, triage, draft, approve, send (doc 07). Crew results arrive on /v1/internal/queries/*."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.auth.deps import require_role, require_service
from app.auth.principal import Principal
from app.core.deps import get_ingest, get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.services import queries as svc
from app.services.cases import load_case

router = APIRouter(tags=["queries"])
Staff = Depends(require_role("desk", "officer"))
Reader = Depends(require_role("desk", "officer", "admin"))
Officer = Depends(require_role("officer"))
Crew = Depends(require_service("svc-crew", "svc-n8n", "svc-internal"))


class TriageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(max_length=40)
    needs_docs: bool = False
    escalation_risk: bool = False
    note: str | None = Field(default=None, max_length=1000)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_text: str = Field(min_length=1, max_length=8000)
    citations: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    attached_doc_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    model_info: dict[str, Any] | None = None


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_text: str = Field(min_length=1, max_length=8000)
    attached_doc_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    expected_version: int | None = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    override_note: str | None = Field(default=None, max_length=1000)


@router.get("/v1/queries", operation_id="listQueries")
async def list_queries(
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> Any:
    if status and status not in ("open", "draft_ready", "answered", "closed", "escalated"):
        raise ApiError("validation_error", "unknown status")
    return await svc.inbox(uow, p, status, limit, cursor)


@router.get("/v1/queries/{query_id}", operation_id="getQuery")
async def get_query(query_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> Any:
    return await svc.detail(uow, p, query_id)


@router.post("/v1/queries/{query_id}/triage", operation_id="overrideTriage")
async def override_triage(
    query_id: str,
    body: TriageIn,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    await svc.visible(uow, p, query_id)
    return await svc.set_triage(
        uow, query_id, body.model_dump(), "officer", p, request.app.state.hub
    )


@router.post("/v1/queries/{query_id}/draft", operation_id="requestQueryDraft", status_code=202)
async def request_draft(
    query_id: str, request: Request, p: Principal = Staff, uow: UoW = Depends(get_uow)
) -> Any:
    q = await svc.visible(uow, p, query_id)
    ok = await request.app.state.crew.start_query_job(
        "query-draft", {"query_id": str(q.id), "case_id": str(q.case_id), "round": q.round}
    )
    if not ok:
        raise ApiError(
            "crew_unavailable",
            "the drafting service is unavailable; write the reply by hand",
            status=503,
        )
    return {"status": "queued"}


@router.put("/v1/queries/{query_id}/response", operation_id="editQueryResponse")
async def edit_response(
    query_id: str, body: EditIn, request: Request, p: Principal = Staff, uow: UoW = Depends(get_uow)
) -> Any:
    return await svc.edit(
        uow,
        p,
        query_id,
        body.draft_text,
        [str(x) for x in body.attached_doc_ids],
        request.app.state.hub,
        body.expected_version,
    )


@router.post("/v1/queries/{query_id}/approve", operation_id="approveQueryResponse")
async def approve(
    query_id: str,
    body: ApproveIn,
    request: Request,
    p: Principal = Officer,
    uow: UoW = Depends(get_uow),
) -> Any:
    return await svc.approve(uow, p, query_id, body.override_note, request.app.state.hub)


@router.post("/v1/queries/{query_id}/send", operation_id="sendQueryResponse")
async def send(
    query_id: str, request: Request, p: Principal = Officer, uow: UoW = Depends(get_uow)
) -> Any:
    return await svc.send(uow, p, query_id, request.app.state.hub, request.app.state.store)


@router.post("/v1/queries/{query_id}/documents", operation_id="uploadQueryDocuments")
async def upload_docs(
    query_id: str,
    files: Annotated[list[UploadFile], File()],
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> Any:
    from app.routers.documents import read_limited
    from app.services import documents as docs

    q = await svc.visible(uow, p, query_id)
    if q.status_t in ("answered", "closed"):
        raise ApiError("query_closed", "the query is no longer open", status=409)
    if len(files) > d.s.max_files_per_case:
        raise ApiError("too_many_files", f"at most {d.s.max_files_per_case} files per request")
    case = await load_case(uow, p, str(q.case_id))
    results = []
    for f in files:
        await docs.rate_limit(d, p)
        data = await read_limited(f, d.s.max_upload_bytes)
        if data is None:
            results.append(
                {
                    "filename": f.filename,
                    "status": "rejected",
                    "error": {"code": "file_too_large", "detail": "file too large"},
                }
            )
            continue
        r = await docs.ingest(uow, d, case, p, f.filename or "file", data, purpose="query")
        if r.get("status") == "accepted" and r.get("doc_id"):
            await uow.session.execute(
                text("UPDATE document SET supplementary=true WHERE id=:i"),
                {"i": uuid.UUID(r["doc_id"])},
            )
            await uow.commit()
        results.append(r)
    return {"results": results}


# ---- crew results ------------------------------------------------------------------------------------------
@router.post("/v1/internal/queries/{query_id}/triage-result", operation_id="queryTriageResult")
async def triage_result(
    query_id: str,
    body: TriageIn,
    request: Request,
    _: Principal = Crew,
    uow: UoW = Depends(get_uow),
) -> Any:
    return await svc.set_triage(
        uow, query_id, body.model_dump(), "crew", None, request.app.state.hub
    )


@router.post("/v1/internal/queries/{query_id}/draft-result", operation_id="queryDraftResult")
async def draft_result(
    query_id: str, body: DraftIn, request: Request, _: Principal = Crew, uow: UoW = Depends(get_uow)
) -> Any:
    b = body.model_dump(mode="json")
    return await svc.draft_result(uow, query_id, b, request.app.state.hub)


@router.post("/v1/internal/jobs/query-overdue", operation_id="jobQueryOverdue")
async def overdue(request: Request, _: Principal = Crew, uow: UoW = Depends(get_uow)) -> Any:
    return await svc.overdue_job(uow, request.app.state.hub)
