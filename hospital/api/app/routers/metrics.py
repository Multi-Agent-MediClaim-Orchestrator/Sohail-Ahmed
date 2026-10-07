"""Scrape endpoint for Prometheus: service accounts and admins only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.auth.deps import require_access
from app.auth.principal import Principal
from app.core import metrics
from app.core.deps import get_uow
from app.core.uow import UoW

router = APIRouter(tags=["metrics"])
Scraper = Depends(require_access(humans=("admin",), services=("svc-internal", "svc-n8n")))


@router.get("/v1/metrics", operation_id="metrics", include_in_schema=True)
async def scrape(_: Principal = Scraper, uow: UoW = Depends(get_uow)) -> Response:
    s = uow.session
    gauges: dict[str, float] = {}
    for st, n in (
        await s.execute(text("SELECT status::text, count(*) FROM outbox GROUP BY 1"))
    ).all():
        gauges[f'outbox_rows{{status="{st}"}}'] = float(n)
    gauges["queries_open"] = float(
        (
            await s.execute(
                text("SELECT count(*) FROM insurer_query WHERE status IN ('open','draft_ready')")
            )
        ).scalar()
        or 0
    )
    gauges["doc_requests_open"] = float(
        (await s.execute(text("SELECT count(*) FROM doc_request WHERE status='open'"))).scalar()
        or 0
    )
    return Response(metrics.render(gauges), media_type="text/plain; version=0.0.4")
