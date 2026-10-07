"""Async engine / session factory. Repos never commit; the service layer owns the transaction boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .settings import Settings, get_settings

_engine: AsyncEngine | None = None
_sm: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str, *, pool_size: int = 10, echo: bool = False) -> AsyncEngine:
    kw: dict[str, object] = {"echo": echo}
    if not url.startswith("sqlite"):
        kw.update(pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)
    return create_async_engine(url, **kw)


def init_db(settings: Settings | None = None, url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _engine, _sm
    s = settings or get_settings()
    _engine = make_engine(url or s.database_url, pool_size=s.db_pool_size, echo=s.db_echo)
    _sm = async_sessionmaker(_engine, expire_on_commit=False)
    return _sm


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sm is None:
        return init_db()
    return _sm


async def dispose_db() -> None:
    global _engine, _sm
    if _engine is not None:
        await _engine.dispose()
    _engine = _sm = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """``async with session_scope() as s`` -> one transaction, committed on success, rolled back on error."""
    sm = sessionmaker()
    async with sm() as s, s.begin():
        yield s


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a session whose transaction is controlled by the service layer."""
    sm = sessionmaker()
    async with sm() as s:
        yield s


class Tx:
    """A transaction with post-commit hooks (events, jobs) so side effects never announce rolled-back state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.after_commit: list[object] = []

    def on_commit(self, fn: object) -> None:
        self.after_commit.append(fn)


@asynccontextmanager
async def transaction(sm: async_sessionmaker[AsyncSession] | None = None) -> AsyncIterator[Tx]:
    """``async with transaction() as tx:`` -> commit, then run ``tx.after_commit`` hooks (sync or async callables)."""
    import inspect

    maker = sm or sessionmaker()
    async with maker() as s:
        tx = Tx(s)
        async with s.begin():
            yield tx
        for fn in tx.after_commit:
            res = fn() if callable(fn) else None  # type: ignore[operator]
            if inspect.isawaitable(res):
                await res
