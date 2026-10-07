"""Routers added by later steps (cases, documents, ...) are listed here."""

from __future__ import annotations

from typing import Any


def routers() -> list[Any]:
    out: list[Any] = []
    for mod in (
        "cases",
        "documents",
        "internal_documents",
        "completeness",
        "admin_config",
        "route",
        "claims",
        "callbacks",
        "queries",
        "internal_ops",
        "stream",
    ):
        try:
            m = __import__(f"app.routers.{mod}", fromlist=["router"])
        except ModuleNotFoundError as e:
            if e.name != f"app.routers.{mod}":
                raise
            continue
        out.append(m.router)
    return out
