"""Service-to-service callbacks (n8n, crew, doc-pipeline, vision) and scheduled jobs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.auth.deps import require_service
from app.auth.principal import Principal
from app.core.deps import get_ingest, get_uow
from app.core.uow import UoW
from app.schemas.documents import ClassifyIn, ParseIn, QualityIn, SplitIn, StatusIn
from app.services import documents as svc

router = APIRouter(prefix="/v1/internal", tags=["internal"])
Svc = Depends(require_service("svc-n8n", "svc-crew", "svc-internal"))
N8n = Depends(require_service("svc-n8n", "svc-internal"))


@router.get("/documents/pending-parse", operation_id="pendingParse")
async def pending(
    older_than_s: int = Query(120, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: Principal = N8n,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    return await svc.pending_parse(uow, older_than_s, limit)


@router.post("/documents/{doc_id}/quality", operation_id="cbQuality")
async def quality(
    doc_id: str,
    body: QualityIn,
    p: Principal = Svc,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.cb_quality(uow, d, p, doc_id, body)


@router.post("/documents/{doc_id}/parse", operation_id="cbParse")
async def parse(
    doc_id: str,
    body: ParseIn,
    p: Principal = Svc,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.cb_parse(uow, d, p, doc_id, body)


@router.post("/documents/{doc_id}/classify", operation_id="cbClassify")
async def classify(
    doc_id: str,
    body: ClassifyIn,
    p: Principal = Svc,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.cb_classify(uow, d, p, doc_id, body)


@router.post("/documents/{doc_id}/status", operation_id="cbStatus")
async def status(
    doc_id: str,
    body: StatusIn,
    p: Principal = Svc,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.cb_status(uow, d, p, doc_id, body)


@router.post("/documents/{doc_id}/split", operation_id="cbSplit")
async def split(
    doc_id: str,
    body: SplitIn,
    p: Principal = Svc,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.cb_split(uow, d, p, doc_id, body)


@router.post("/jobs/sweeper", operation_id="jobSweeper")
async def job_sweeper(
    _: Principal = N8n, uow: UoW = Depends(get_uow), d: Any = Depends(get_ingest)
) -> dict[str, int]:
    return await svc.sweeper(uow, d)


@router.post("/jobs/purge-tombstones", operation_id="jobPurge")
async def job_purge(
    _: Principal = N8n, uow: UoW = Depends(get_uow), d: Any = Depends(get_ingest)
) -> dict[str, int]:
    return await svc.purge_tombstones(uow, d)


@router.post("/jobs/stale-drafts", operation_id="jobStale")
async def job_stale(_: Principal = N8n, uow: UoW = Depends(get_uow)) -> dict[str, int]:
    return await svc.flag_stale_drafts(uow)


@router.post("/jobs/orphan-scan", operation_id="jobOrphans")
async def job_orphans(
    _: Principal = N8n, uow: UoW = Depends(get_uow), d: Any = Depends(get_ingest)
) -> dict[str, Any]:
    return await svc.orphan_scan(uow, d)
