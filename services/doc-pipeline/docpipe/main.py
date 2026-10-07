"""doc-pipeline service (port 8200 by default). Localhost only; callers: hospital n8n flows and hospital-api."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from docpipe.jobs import Jobs
from docpipe.llm import LLM, Ollama
from docpipe.pipeline import run
from docpipe.settings import PIPELINE_VERSION, Settings
from docpipe.stages import mask
from docpipe.stages.render import ParseError, render, sniff


class ParseReq(BaseModel):
    model_config = ConfigDict(extra="ignore")
    document_id: str | None = None
    case_id: str | None = None
    url: str = Field(min_length=8)
    doc_type_hint: str | None = None
    force_second_pass: bool = False


class MaskReq(BaseModel):
    text: str = Field(max_length=200_000)


def create_app(
    settings: Settings | None = None, *, llm: LLM | None = None, fetch: Any = None
) -> FastAPI:
    s = settings or Settings.from_env()
    llm = llm or Ollama(s.llm_base_url, s.llm_timeout_s)
    client = httpx.AsyncClient(timeout=60)

    async def get_bytes(url: str) -> bytes:
        if fetch is not None:
            return await fetch(url)  # type: ignore[no-any-return]
        r = await client.get(url)
        if r.status_code >= 400:
            raise ParseError("object_not_found", f"HTTP {r.status_code}")
        return r.content

    async def job_fn(body: dict[str, Any]) -> dict[str, Any]:
        raw = body["raw"] if "raw" in body else await get_bytes(body["url"])
        out = await run(
            raw,
            s,
            llm,
            doc_type_hint=body.get("doc_type_hint"),
            force_second_pass=bool(body.get("force_second_pass")),
        )
        out["document_id"] = body.get("document_id")
        return out

    jobs = Jobs(job_fn, s.workers)
    app = FastAPI(title="doc-pipeline", version=PIPELINE_VERSION)
    app.state.jobs, app.state.settings = jobs, s

    @app.post("/v1/parse", status_code=202)
    async def parse(req: ParseReq) -> dict[str, str]:
        return {"job_id": jobs.submit(req.model_dump())}

    @app.post("/v1/parse/upload", status_code=202)
    async def parse_upload(
        file: UploadFile = File(...), doc_type_hint: str | None = Form(None)
    ) -> dict[str, str]:
        raw = await file.read()
        try:
            sniff(raw)
        except ParseError as e:
            raise HTTPException(422, e.code) from e
        return {"job_id": jobs.submit({"raw": raw, "doc_type_hint": doc_type_hint})}

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        j = jobs.jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "unknown job")
        return {k: v for k, v in j.items() if k != "finished"}

    @app.post("/v1/classify")
    async def classify_only(file: UploadFile = File(...)) -> dict[str, Any]:
        from docpipe.stages import classify

        rend = render(await file.read(), s.parser, s.max_pages, s.textlayer_min_chars)
        dt, conf, decisive = classify.classify("\n".join(p.text for p in rend.pages))
        return {"doc_type": dt, "doc_type_conf": conf, "decisive": decisive}

    @app.post("/v1/mask")
    async def mask_text(req: MaskReq) -> dict[str, Any]:
        m = mask.mask(req.text)  # the map is discarded: this endpoint only returns masked text
        return {
            "masked_text": m.text,
            "entities": [{"type": t.strip("<>").rsplit("_", 1)[0], "token": t} for t in m.pii_map],
        }

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": PIPELINE_VERSION,
            "parser": s.parser,
            "models": {"local": s.model_local, "cloud": s.model_cloud},
        }

    return app


def app_factory() -> FastAPI:
    return create_app()
