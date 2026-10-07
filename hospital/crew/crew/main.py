"""hospital-crew: FastAPI wrapper around the agents (doc 09 §4). Binds to localhost; the API/n8n are the only
callers. Results go back to the hospital API with the crew's own service token."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from crew.agents import builder, responder
from crew.api_client import ApiClient, HttpApi
from crew.jobs import Job, Runner
from crew.llm import LLM, OllamaLLM, RulesLLM
from crew.settings import Settings

JOB_TYPES = ("claim-build", "claim-repair", "query-triage", "query-draft")


class JobIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str | None = Field(default=None, max_length=64)
    case_id: str | None = None
    query_id: str | None = None
    correlation_id: str | None = None


def flat(info: dict[str, Any]) -> dict[str, str | int | float | None]:
    return {k: v for k, v in info.items() if isinstance(v, (str, int, float)) or v is None}


def create_app(
    settings: Settings | None = None, *, llm: LLM | None = None, api: ApiClient | None = None
) -> FastAPI:
    st = settings or Settings.from_env()
    llm = llm or (
        RulesLLM() if st.llm_mode == "rules" else OllamaLLM(st.llm_base_url, st.llm_timeout_s)
    )
    api = api or HttpApi(st.api_url, st.token_url, st.client_id, st.client_secret)

    async def claim_build(job: Job) -> dict[str, Any]:
        i = job.input
        ctx = await api.get(f"/v1/internal/cases/{i['case_id']}/build-context")
        rep = i.get("repair")
        if rep:
            prev = ctx.get("previous_draft")
            if prev is None:
                raise ValueError("no previous draft to repair")
            out = await builder.repair(prev, rep.get("errors", []), llm, st)
            rnd = int(rep.get("round", 1))
        else:
            out = await builder.build(ctx, llm, st)
            rnd = 0
        if job.cancel.is_set():
            return {"cancelled": True}
        body = {"job_id": job.id if not i.get("job_id") else i["job_id"], "payload": out["payload"], "provenance": out["provenance"],
                "model_info": flat(out["model_info"]), "repair_round": rnd}  # fmt: skip
        await api.post(i.get("callback") or f"/v1/internal/cases/{i['case_id']}/claim/draft", body)
        return {"posted": True, "repair_round": rnd}

    async def query_triage(job: Job) -> dict[str, Any]:
        q = await api.get(f"/v1/internal/queries/{job.input['query_id']}")
        out = await responder.triage(q, llm, st)
        mi = out.pop("_model_info")
        await api.post(f"/v1/internal/queries/{q['id']}/triage-result", out)
        return out | {"model_info": mi}

    async def query_draft(job: Job) -> dict[str, Any]:
        qid = job.input["query_id"]
        ctx = await api.get(f"/v1/internal/queries/{qid}/context")
        out = await responder.draft(ctx, llm, st)
        body = {"draft_text": out["draft_text"], "citations": out["citations"], "attached_doc_ids": ctx.get("attach_doc_ids", []),
                "model_info": flat(out["model_info"] | {"supervisor_pass": int(out["supervisor"]["pass"])})}  # fmt: skip
        await api.post(f"/v1/internal/queries/{qid}/draft-result", body)
        return {"supervisor": out["supervisor"], "unsupported": out["unsupported"]}

    runner = Runner({"claim-build": claim_build, "claim-repair": claim_build, "query-triage": query_triage, "query-draft": query_draft},
                    st.concurrency, st.job_ttl_s)  # fmt: skip
    app = FastAPI(title="hospital-crew", version="0.1.0")
    app.state.runner, app.state.settings = runner, st

    @app.post("/v1/jobs/{kind}", status_code=202)
    async def submit(kind: str, body: JobIn) -> dict[str, str]:
        if kind not in JOB_TYPES:
            raise HTTPException(404, "unknown job type")
        data = body.model_dump()
        need = "query_id" if kind.startswith("query") else "case_id"
        if not data.get(need):
            raise HTTPException(422, f"{need} is required")
        return {"job_id": runner.submit(kind, data).id}

    @app.get("/v1/jobs")
    async def list_jobs() -> dict[str, Any]:
        """Latest jobs with their errors, for diagnostics (no payloads)."""
        rows = sorted(runner.jobs.values(), key=lambda j: j.created_at)[-20:]
        return {
            "items": [
                {"job_id": j.id, "type": j.type, "state": j.state, "error": j.error} for j in rows
            ]
        }

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        j = runner.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "unknown job")
        return j.envelope()

    @app.delete("/v1/jobs/{job_id}")
    async def cancel(job_id: str) -> dict[str, bool]:
        if not runner.cancel(job_id):
            raise HTTPException(404, "unknown job")
        return {"cancelled": True}

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "models": {"general": st.model_general, "local": st.model_local},
        }

    @app.get("/v1/agents")
    async def agents() -> dict[str, Any]:
        from crew import prompts

        return {"prompts": {n: prompts.load(st.prompt_dir, n, st.prompt_pins).version for n in ("map_category", "triage", "draft_reply", "supervise")},
                "jobs": list(JOB_TYPES)}  # fmt: skip

    return app


def app_factory() -> FastAPI:
    return create_app()
