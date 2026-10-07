"""Alembic environment. Uses the OWNER connection (``INS_OWNER_DATABASE_URL``), never the app role."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("INS_OWNER_DATABASE_URL", "")
    if not url:
        raise RuntimeError("set INS_OWNER_DATABASE_URL (or sqlalchemy.url)")
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql://", "postgresql+psycopg2://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=None, version_table_schema="public")
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
