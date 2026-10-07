"""Dashboard numbers: one cheap query set per role scope."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.auth.deps import case_scope_sql, require_role
from app.auth.principal import Principal
from app.core.deps import get_uow
from app.core.uow import UoW

router = APIRouter(tags=["dashboard"])


@router.get("/v1/dashboard/summary", operation_id="dashboardSummary")
async def summary(
    p: Principal = Depends(require_role("desk", "officer")), uow: UoW = Depends(get_uow)
) -> dict[str, Any]:
    sql, params = case_scope_sql(p)
    s = uow.session
    by_status = {
        r.status: r.n
        for r in (
            await s.execute(
                text(
                    f"SELECT c.status::text AS status, count(*) AS n FROM claim_case c WHERE {sql} GROUP BY 1"
                ),  # noqa: S608
                params,
            )
        ).all()
    }
    q = {
        r.status: r.n
        for r in (
            await s.execute(
                text(
                    f"SELECT q.status::text AS status, count(*) AS n FROM insurer_query q JOIN claim_case c ON c.id=q.case_id WHERE {sql} GROUP BY 1"  # noqa: S608
                ),
                params,
            )
        ).all()
    }
    overdue_q = (
        await s.execute(
            text(
                f"SELECT count(*) FROM insurer_query q JOIN claim_case c ON c.id=q.case_id WHERE {sql} "  # noqa: S608
                "AND q.status IN ('open','draft_ready') AND q.due_by < now()"
            ),
            params,
        )
    ).scalar()
    overdue_req = (
        await s.execute(
            text(
                f"SELECT count(*) FROM doc_request r JOIN claim_case c ON c.id=r.case_id WHERE {sql} "  # noqa: S608
                "AND r.status='open' AND r.due_by < now()"
            ),
            params,
        )
    ).scalar()
    deadlines = (
        await s.execute(
            text(
                f"SELECT c.id, c.claim_ref, c.filing_deadline FROM claim_case c WHERE {sql} AND c.filing_deadline IS NOT NULL "  # noqa: S608
                "AND c.filing_deadline <= current_date + 7 AND c.status::text IN ('draft','docs_pending','docs_complete','building_claim','ready_for_review') "
                "ORDER BY c.filing_deadline LIMIT 10"
            ),
            params,
        )
    ).all()
    return {
        "cases_by_status": by_status,
        "queries_by_status": q,
        "overdue_queries": overdue_q,
        "overdue_requests": overdue_req,
        "deadlines": [
            {
                "case_id": str(r.id),
                "claim_ref": r.claim_ref,
                "filing_deadline": r.filing_deadline.isoformat(),
            }
            for r in deadlines
        ],
    }
