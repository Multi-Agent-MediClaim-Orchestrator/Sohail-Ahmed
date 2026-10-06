"""Alembic environment: sync psycopg as hosp_owner. SQL-first migrations; the model-vs-DB drift
check (`alembic check`) compares tables, columns, types, nullability, FKs and unique constraints.
Indexes (partial/GIN) and CHECK constraints live in the migrations and are not diffed."""

import app.models  # noqa: F401  (registers tables on Base.metadata)
from alembic import context
from app.db.base import Base
from app.db.urls import owner_url
from sqlalchemy import create_engine

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "index":
        return False  # partial / GIN / trigram indexes are defined in SQL migrations
    return True


def run_migrations_online() -> None:
    url = config.attributes.get("url") or owner_url()
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
