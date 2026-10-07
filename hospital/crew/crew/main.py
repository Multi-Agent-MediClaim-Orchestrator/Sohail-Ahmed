"""hospital-crew: FastAPI wrapper around the CrewAI flows (doc 09 §4). Binds to localhost; the API/n8n are the only
callers. Each job runs one CrewAI Flow (crew/flows.py); results go back to the hospital API with the crew's own
service token."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from crew.api_client import ApiClient, HttpApi
from crew.flows import ClaimFlow, FlowDeps, QueryFlow
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
        flow = ClaimFlow.create(FlowDeps(llm, api, st, job.cancel))
        return await flow.kickoff_async(inputs={"case_id": i["case_id"], "job_id": i.get("job_id") or job.id,
                                                "callback": i.get("callback"), "repair": i.get("repair")})  # fmt: skip

    def query_job(kind: str):  # noqa: ANN202
        async def run(job: Job) -> dict[str, Any]:
            flow = QueryFlow.create(FlowDeps(llm, api, st, job.cancel))
            return await flow.kickoff_async(inputs={"query_id": job.input["query_id"], "kind": kind})

        return run

    query_triage, query_draft = query_job("triage"), query_job("draft")

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
        from crew.team import AGENTS

        return {"prompts": {n: prompts.load(st.prompt_dir, n, st.prompt_pins).version for n in ("map_category", "triage", "draft_reply", "supervise")},
                "jobs": list(JOB_TYPES), "framework": "crewai",
                "agents": {k: {"role": v["role"], "prompt": v["prompt"]} for k, v in AGENTS.items()},
                "flows": {"ClaimFlow": ["claim-build", "claim-repair"], "QueryFlow": ["query-triage", "query-draft"]}}  # fmt: skip

    return app


def app_factory() -> FastAPI:
    return create_app()
