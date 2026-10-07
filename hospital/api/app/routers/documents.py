from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth.deps import require_role
from app.auth.principal import Principal
from app.core.deps import get_ingest, get_uow
from app.core.errors import ApiError
from app.core.uow import UoW
from app.schemas.documents import DocTypePatch
from app.services import documents as svc
from app.services import previews
from app.services.cases import load_case
from app.services.route_triggers import request_state, route_on_doc

router = APIRouter(tags=["documents"])
Staff = Depends(require_role("desk", "officer"))
Reader = Depends(require_role("desk", "officer", "admin"))
CHUNK = 1024 * 1024


async def read_limited(f: UploadFile, limit: int) -> bytes | None:
    """Read at most `limit` bytes (checks actual bytes, not the declared Content-Length)."""
    buf = bytearray()
    while chunk := await f.read(CHUNK):
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


@router.post("/v1/cases/{case_id}/documents", status_code=202, operation_id="uploadDocuments")
async def upload(
    case_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    doc_type_hint: Annotated[list[str] | None, Form()] = None,
    supersedes_id: Annotated[list[str] | None, Form()] = None,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> Any:
    case = await load_case(uow, p, case_id)
    if len(files) > d.s.max_files_per_case:
        raise ApiError("too_many_files", f"at most {d.s.max_files_per_case} files per request")
    hints = doc_type_hint or []
    sups = supersedes_id or []
    results: list[dict[str, Any]] = []
    for i, f in enumerate(files):
        await svc.rate_limit(d, p)
        data = await read_limited(f, d.s.max_upload_bytes)
        name = f.filename or "file"
        if data is None:
            results.append(
                {
                    "filename": svc.filechecks.sanitize_filename(name),
                    "status": "rejected",
                    "error": {
                        "code": "file_too_large",
                        "detail": f"file exceeds {d.s.max_upload_mb} MB",
                    },
                }
            )
            continue
        hint = hints[i] if i < len(hints) and hints[i] else None
        if hint is not None:
            from claim_contract.enums import DocType

            if hint not in {t.value for t in DocType}:
                results.append(
                    {
                        "filename": name,
                        "status": "rejected",
                        "error": {
                            "code": "validation_error",
                            "detail": f"unknown doc_type_hint {hint}",
                        },
                    }
                )
                continue
        results.append(
            await svc.ingest(
                uow,
                d,
                case,
                p,
                name,
                data,
                hint=hint,
                supersedes_id=sups[i] if i < len(sups) and sups[i] else None,
            )
        )
        case = await load_case(uow, p, case_id)  # status may have changed (draft -> docs_pending)
    ok = [r for r in results if r["status"] in ("accepted", "skipped_duplicate")]
    return JSONResponse({"documents": results}, status_code=202 if ok else 422)


@router.get("/v1/cases/{case_id}/documents", operation_id="listDocuments")
async def list_documents(
    case_id: str,
    lifecycle: str = Query("active", pattern="^(active|all)$"),
    doc_type: str | None = None,
    needs_attention: bool | None = None,
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
) -> dict[str, Any]:
    return await svc.list_docs(uow, p, case_id, lifecycle, doc_type, needs_attention)


@router.get("/v1/documents/{doc_id}", operation_id="getDocument")
async def get_document(
    doc_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    row = await svc.get_doc_row(uow, p, doc_id)
    out = svc.doc_view(row, row.uploader_name)
    passes = await svc.parse_detail(uow, p, doc_id)
    out["agreement_score"] = passes["agreement_score"]
    return out


@router.get("/v1/documents/{doc_id}/download", operation_id="downloadDocument")
async def download(
    doc_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow), d: Any = Depends(get_ingest)
) -> Response:
    return RedirectResponse(await svc.presign(uow, d, p, doc_id), status_code=302)


@router.get("/v1/documents/{doc_id}/pages/{n}", operation_id="documentPage")
async def page(
    doc_id: str,
    n: int,
    p: Principal = Reader,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> Response:
    row = await svc.get_doc_row(uow, p, doc_id)
    key = f"{row.case_id}/{row.id}/pages/{n}.png"
    await previews.ensure(d, row, n, key)  # rendered on first view, then cached in the object store
    return RedirectResponse(await svc.presign(uow, d, p, doc_id, key), status_code=302)


@router.get("/v1/documents/{doc_id}/parse", operation_id="getParse")
async def parse(doc_id: str, p: Principal = Reader, uow: UoW = Depends(get_uow)) -> dict[str, Any]:
    return await svc.parse_detail(uow, p, doc_id)


@router.patch("/v1/documents/{doc_id}", operation_id="patchDocument")
async def patch_document(
    doc_id: str,
    body: DocTypePatch,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    out = await svc.reclassify(uow, d, p, doc_id, body.doc_type.value)
    await route_on_doc(uow, request_state(d), out["case_id"], body.doc_type.value, p.actor_id)
    return out


@router.delete("/v1/documents/{doc_id}", status_code=204, operation_id="deleteDocument")
async def delete(
    doc_id: str, p: Principal = Staff, uow: UoW = Depends(get_uow), d: Any = Depends(get_ingest)
) -> Response:
    await svc.delete_doc(uow, d, p, doc_id)
    return Response(status_code=204)


@router.post("/v1/documents/{doc_id}/reparse", status_code=202, operation_id="reparseDocument")
async def reparse(
    doc_id: str,
    full: bool = False,
    p: Principal = Staff,
    uow: UoW = Depends(get_uow),
    d: Any = Depends(get_ingest),
) -> dict[str, Any]:
    return await svc.reparse(uow, d, p, doc_id, full)
