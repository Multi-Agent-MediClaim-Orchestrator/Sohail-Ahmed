"""Unit of work: one DB transaction per request, plus callbacks that run only after commit
(SSE publishes, n8n triggers) so a rollback never emits events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession


class UoW:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._after: list[Callable[[], Awaitable[None]]] = []

    def after_commit(self, cb: Callable[[], Awaitable[None]]) -> None:
        self._after.append(cb)

    async def commit(self) -> None:
        await self.session.commit()
        after, self._after = self._after, []
        for cb in after:
            try:
                await cb()
            except Exception:  # noqa: BLE001  post-commit side effects must not fail the request
                import logging

                logging.getLogger("app.uow").exception("after_commit callback failed")

    async def rollback(self) -> None:
        self._after.clear()
        await self.session.rollback()
