from __future__ import annotations

from typing import Any


def run(op: Any, *statements: str) -> None:
    for s in statements:
        op.execute(s)


def add_updated_at_trigger(op: Any, schema: str, table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated BEFORE UPDATE ON {schema}.{table} "
        "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
    )


def drop_updated_at_trigger(op: Any, schema: str, table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated ON {schema}.{table}")
