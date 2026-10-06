"""Glue that fires router recomputation from document events without import cycles."""

from __future__ import annotations

from typing import Any

from app.core.uow import UoW
from app.router_engine import service as router_svc

NOTE_TYPES = {"fir_mlc", "admission_note", "preauth_approval"}


def request_state(d: Any) -> Any:
    """The Ingest bundle already carries hub and completeness hook."""
    return d


async def route_on_doc(uow: UoW, d: Any, case_id: str, doc_type: str, actor: str) -> None:
    if doc_type in NOTE_TYPES:
        await router_svc.recompute(
            uow, case_id, "doc_classified", actor, hub=d.hub, completeness=d.completeness
        )
