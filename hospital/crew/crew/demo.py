"""Stand-alone demo of the hospital crew's CrewAI flows on built-in synthetic data, with no API, database or n8n:

    cd hospital/crew && crewai run              # same as `uv run kickoff`; CREW_LLM=rules (no model) by default
    CREW_LLM=ollama crewai run                  # real agents on the local Ollama model (HOSP_LLM_* settings)
    uv run plot                                 # writes the flow diagrams (HTML) into the current folder

The in-memory API below answers the flows' reads and prints what they would post to hospital-api."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from crew.flows import ClaimFlow, FlowDeps, QueryFlow
from crew.llm import LLM, OllamaLLM, RulesLLM
from crew.settings import Settings

CASE = "c-demo-1"
QUERY = "q-demo-1"
BUILD_CONTEXT = {
    "case": {
        "patient": {"full_name": "Demo Patient", "dob": "1984-03-12", "gender": "M", "member_id": "M-1", "policy_number": "P-1"},
        "admission": {"admission_type": "planned", "admitted_on": "2026-09-28", "discharged_on": "2026-10-02",
                      "diagnosis_codes": ["K35.80"], "procedure_codes": [], "treating_doctor": "Dr. Rao",
                      "hospital_id": None, "preauth_ref": None},
    },
    "documents": [
        {"id": "11111111-1111-4111-8111-111111111111", "doc_type": "final_bill", "pages": 1,
         "typed": {"lines": [{"description": "Room rent", "qty": "4", "unit_price": "2500", "amount": "10000"},
                             {"description": "Laparoscopic appendectomy", "amount": "45000"},
                             {"description": "Ward attendant service charge", "amount": "800"}]}},
        {"id": "22222222-2222-4222-8222-222222222222", "doc_type": "prescription", "pages": 1, "typed": {}},
    ],
}  # fmt: skip
QUERY_ROW = {"id": QUERY, "category": "billing_discrepancy", "round": 1,
             "text": "Please explain the room rent of 10000.00 billed for four days."}  # fmt: skip
QUERY_CONTEXT = {
    "query_id": QUERY, "round": 1, "category": "billing_discrepancy",
    "evidence": [QUERY_ROW["text"], '{"bill_lines": [{"description": "Room rent", "qty": "4", "unit_price": "2500", "amount": "10000.00"}]}'],
    "attach_doc_ids": [],
}  # fmt: skip


class MemoryApi:
    def __init__(self) -> None:
        self.data = {
            f"/v1/internal/cases/{CASE}/build-context": BUILD_CONTEXT,
            f"/v1/internal/queries/{QUERY}": QUERY_ROW,
            f"/v1/internal/queries/{QUERY}/context": QUERY_CONTEXT,
        }

    async def get(self, path: str) -> dict[str, Any]:
        return self.data[path]

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        print(f"\n--> POST {path}\n{json.dumps(body, indent=2, ensure_ascii=False)[:1500]}")
        return {}


def _llm(st: Settings) -> LLM:
    return RulesLLM() if st.llm_mode == "rules" else OllamaLLM(st.llm_base_url, st.llm_timeout_s)


async def _run() -> None:
    st = Settings.from_env()
    if "CREW_LLM" not in os.environ:
        st = replace(st, llm_mode="rules")
    print(f"hospital crew demo: CrewAI flows, model mode = {st.llm_mode}")
    deps = FlowDeps(_llm(st), MemoryApi(), st)
    for title, flow, inputs in (
        (
            "ClaimFlow (claim-build)",
            ClaimFlow.create(deps, quiet=False),
            {"case_id": CASE, "job_id": "demo-build"},
        ),
        (
            "QueryFlow (query-triage)",
            QueryFlow.create(deps, quiet=False),
            {"query_id": QUERY, "kind": "triage"},
        ),
        (
            "QueryFlow (query-draft)",
            QueryFlow.create(deps, quiet=False),
            {"query_id": QUERY, "kind": "draft"},
        ),
    ):
        print(f"\n===== {title} =====")
        result = await flow.kickoff_async(inputs=inputs)
        print(
            f"steps: {' -> '.join(flow.state.steps)}\nresult: {json.dumps(result, default=str)[:600]}"
        )


def kickoff() -> None:
    os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
    asyncio.run(_run())


def plot() -> None:
    for flow, name in (
        (ClaimFlow(), "hospital_claim_flow.html"),
        (QueryFlow(), "hospital_query_flow.html"),
    ):
        src = Path(flow.plot(name, show=False))
        for (
            f
        ) in src.parent.iterdir():  # the page and its js/css, copied next to where the command runs
            shutil.copy(f, Path.cwd() / f.name)
        print(Path.cwd() / name)


if __name__ == "__main__":
    kickoff()
