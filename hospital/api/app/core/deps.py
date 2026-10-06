from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from app.core.config import Settings
from app.core.uow import UoW


def settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_uow(request: Request) -> AsyncIterator[UoW]:
    async with request.app.state.sessionmaker() as session:
        uow = UoW(session)
        try:
            yield uow
        finally:
            await uow.rollback()  # no-op after a successful commit


def get_redis(request: Request) -> Any:
    return request.app.state.redis


def get_hub(request: Request) -> Any:
    return request.app.state.hub


def get_ingest(request: Request) -> Any:
    from app.services.documents import Ingest

    st = request.app.state
    return Ingest(st.settings, st.store, st.clam, st.n8n, st.hub, st.redis, st.completeness)
