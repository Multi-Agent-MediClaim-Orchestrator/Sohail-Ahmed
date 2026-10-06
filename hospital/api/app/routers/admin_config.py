from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.deps import require_role
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.uow import UoW
from app.services import config_admin as svc

router = APIRouter(prefix="/v1/admin/config", tags=["admin-config"])
Admin = Depends(require_role("admin"))


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]
    change_note: str = Field(min_length=3, max_length=500)
    name: str = "default"


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: datetime | None = None
    name: str = "default"


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample: str = "last_100_cases"
    compare_to: str = "published"


class ReevalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_ids: list[str] | None = None
    all_open: bool = False


@router.get("/{domain}", operation_id="listConfigVersions")
async def list_versions(
    domain: str, name: str = "default", _: Principal = Admin, uow: UoW = Depends(get_uow)
) -> Any:
    return await svc.list_versions(uow, domain, name)


@router.get("/{domain}/schema", operation_id="configSchema")
async def schema(domain: str, _: Principal = Admin) -> Any:
    from app.core.errors import ApiError

    if domain not in svc.DOMAINS:
        raise ApiError("not_found", "unknown config domain")
    return svc.schema_for(domain)


@router.post("/{domain}", status_code=201, operation_id="createConfigDraft")
async def create(
    domain: str, body: DraftBody, p: Principal = Admin, uow: UoW = Depends(get_uow)
) -> Any:
    return await svc.create_draft(uow, p, domain, body.payload, body.change_note, body.name)


@router.post("/{domain}/reevaluate", operation_id="reevaluateCases")
async def reevaluate(
    domain: str,
    body: ReevalBody,
    request: Request,
    p: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    from app.core.errors import ApiError

    if domain != "doc_requirements":
        raise ApiError("validation_error", "re-evaluation applies to doc_requirements")
    return await svc.reevaluate(
        uow, p, body.case_ids, body.all_open, request.app.state.hub, request.app.state.settings
    )


@router.get("/{domain}/{version}", operation_id="getConfigVersion")
async def get_version(
    domain: str,
    version: int,
    name: str = "default",
    _: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    row = await svc.get_version(uow, domain, version, name)
    return {**svc._view(row), "payload": row.payload}  # noqa: SLF001


@router.post("/{domain}/{version}/validate", operation_id="validateConfigVersion")
async def validate(
    domain: str,
    version: int,
    name: str = "default",
    p: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    return await svc.validate_version(uow, p, domain, version, name)


@router.post("/{domain}/{version}/dry-run", operation_id="dryRunConfigVersion")
async def dry_run(
    domain: str,
    version: int,
    body: DryRunBody | None = None,
    name: str = "default",
    _: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    n = 100
    if body and body.sample.startswith("last_") and body.sample.endswith("_cases"):
        n = max(1, min(int(body.sample[5:-6] or 100), 1000))
    return await svc.dry_run(uow, domain, version, n, name)


@router.post("/{domain}/{version}/publish", operation_id="publishConfigVersion")
async def publish(
    domain: str,
    version: int,
    request: Request,
    body: PublishBody | None = None,
    p: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    b = body or PublishBody()
    out = await svc.publish(
        uow, p, domain, version, request.app.state.redis, b.name, b.effective_from
    )
    if (
        domain == "router_rules"
    ):  # in-flight draft/docs_pending cases follow the new rules (doc 05 §5.5)
        from app.router_engine import service as router_svc  # noqa: PLC0415

        st = request.app.state
        out["cases_queued"] = await router_svc.count_open_cases(uow)
        st.scheduler.spawn(router_svc.recompute_open_cases_job(request.app, version))
    return out


@router.post("/{domain}/{version}/retire", operation_id="retireConfigVersion")
async def retire(
    domain: str,
    version: int,
    name: str = "default",
    p: Principal = Admin,
    uow: UoW = Depends(get_uow),
) -> Any:
    return await svc.retire(uow, p, domain, version, name)
