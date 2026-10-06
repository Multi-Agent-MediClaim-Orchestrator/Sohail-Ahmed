from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.capabilities import capabilities
from app.auth.deps import require_role
from app.auth.principal import Principal

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me", operation_id="getMe")
async def get_me(
    p: Principal = Depends(require_role("desk", "officer", "admin")),
) -> dict[str, object]:
    return {
        "id": str(p.id),
        "email": p.email,
        "name": p.name,
        "roles": sorted(p.roles),
        "capabilities": capabilities(p.roles),
    }
