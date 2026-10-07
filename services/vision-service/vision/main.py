"""vision-service (port 8300 by default). Localhost only."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from vision.analyze import analyze, hospital_quality
from vision.escalate import Escalator
from vision.imageio import ImageError, load_pages
from vision.registry import Registry
from vision.settings import Settings


class Req(BaseModel):
    model_config = ConfigDict(extra="ignore")
    document_id: str | None = None
    url: str = Field(min_length=8)
    doc_type: str | None = None
    expect: list[str] | None = None
    expect_any: list[list[str]] | None = None


def create_app(
    settings: Settings | None = None,
    *,
    fetch: Any = None,
    registry: Registry | None = None,
    esc: Escalator | None = None,
) -> FastAPI:
    s = settings or Settings.from_env()
    client = httpx.AsyncClient(timeout=60)
    reg = registry or Registry(s)
    escalator = esc or Escalator(s)
    app = FastAPI(title="vision-service", version="1.0.0")

    async def get_bytes(url: str) -> bytes:
        if fetch is not None:
            return await fetch(url)  # type: ignore[no-any-return]
        r = await client.get(url)
        if r.status_code >= 400:
            raise HTTPException(404, "object_not_found")
        return r.content

    def pages_of(raw: bytes) -> Any:
        try:
            return load_pages(raw, s.max_pages, s.max_mp)
        except ImageError as e:
            raise HTTPException(413 if e.code == "image_too_large" else 415, e.code) from e

    async def run(raw: bytes, r: Req, use_esc: bool) -> dict[str, Any]:
        return await analyze(pages_of(raw), s, await reg.get(), doc_type=r.doc_type, expect=r.expect, expect_any=r.expect_any,
                             esc=escalator if use_esc else None, doc_id=r.document_id or "")  # fmt: skip

    @app.post("/v1/quality")
    async def quality_ep(r: Req) -> dict[str, Any]:
        """The body hospital-api stores: {quality_score, flags, has_required_stamp}. No escalation on this cheap path."""
        rep = await run(
            await get_bytes(r.url),
            r.model_copy(update={"expect": r.expect or ["hospital_stamp"], "doc_type": None}),
            False,
        )
        return hospital_quality(rep)

    @app.post("/v1/analyze")
    async def analyze_ep(r: Req) -> dict[str, Any]:
        return await run(await get_bytes(r.url), r, True)

    @app.post("/v1/analyze/upload")
    async def analyze_upload(
        file: UploadFile = File(...), doc_type: str | None = Form(None)
    ) -> dict[str, Any]:
        return await run(await file.read(), Req(url="http://upload", doc_type=doc_type), True)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "1.0.0",
            "detector_mode": "classical",
            "ocr": "tesseract",
            "vision_model": s.vision_model,
        }

    return app


def app_factory() -> FastAPI:
    return create_app()
