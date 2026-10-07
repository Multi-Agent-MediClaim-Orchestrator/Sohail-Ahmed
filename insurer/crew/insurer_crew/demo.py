"""Stand-alone demo of the insurer crew's CrewAI flows on built-in synthetic claims, with no API, database or n8n:

    cd insurer/crew && crewai run          # same as `uv run kickoff`; offline stand-in model (no Ollama needed)
    INS_CREW_LLM=ollama crewai run         # real agents on local Ollama (INS_CREW_MODEL, default gemma4:latest)
    uv run plot                            # writes the flow diagrams (HTML) into the current folder

The flows run through the crew's own HTTP app in-process, so every guard (context validation, PII scan, schema repair,
output checks) applies exactly as in production."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx

from .runtime import GatewayLLM, LLMResult, Settings

MEMBER = {"member_id": "MEM-****0003", "full_name": "Rohan Iyer", "dob": "2016-09-25", "gender": "M", "policy_number": "POL-NIV-2025-000101"}
DOC = {"doc_id": "d1", "doc_type": "id_proof", "pages": 1, "parse_confidence": 0.97}
BILL = {"doc_id": "d2", "doc_type": "final_bill", "pages": 1, "parse_confidence": 0.97}
LINES = [{"line_ref": r, "category": c, "description": d, "qty": "1", "unit_price": a, "amount": a} for r, c, d, a in (
    ("L1", "room", "Room rent general ward", "8000.00"), ("L2", "medicine", "Inj Ceftriaxone 1g", "1200.00"),
    ("L3", "investigation", "Blood test CBC", "600.00"))]  # fmt: skip
AUTH_CLEAN = {"arithmetic": {"total_mismatch": False, "total_diff": "0.00", "line_mismatches": []}, "duplicates": {"exact": [], "near": []},
              "stamp_reports": [{"doc_id": "d2", "stamp_present": True, "confidence": 0.93}], "vision_reports": [{"doc_id": "d2", "quality": "good"}],
              "docs": [BILL]}  # fmt: skip


def identity_ctx(name: str, dob: str, score: float) -> dict[str, Any]:
    return {"member": MEMBER, "patient": {"full_name": name, "dob": dob, "gender": "M", "policy_number": MEMBER["policy_number"]},
            "deterministic_facts": {"name_score": score, "dob_match": dob == MEMBER["dob"], "policy_match": True, "member_found": True},
            "docs": [{**DOC, "extract_masked": {"patient_name": name, "dob": dob}}]}  # fmt: skip


CLAIMS = {
    "clean claim": {"identity": identity_ctx("Rohan Iyer", "2016-09-25", 1.0), "authenticity": AUTH_CLEAN,
                    "calc_mapper": {"lines": LINES, "product_code": "HF-GOLD"}},
    "identity mismatch": {"identity": identity_ctx("Suresh Verma", "1979-03-02", 0.12), "authenticity": AUTH_CLEAN,
                          "calc_mapper": {"lines": LINES, "product_code": "HF-GOLD"}},
}  # fmt: skip
QUERY = {"round": 1, "claim_ref": "HC-2026-000123", "hospital_name": "City Care Hospital",
         "findings": [{"key": "f1", "kind": "completeness.missing_required", "severity": "blocker", "detail": "pharmacy_bill", "requested_doc_type": "pharmacy_bill"}],
         "requirements": [{"doc_type": "pharmacy_bill", "rule": "required for every claim"}]}  # fmt: skip
REPLY = {"open_findings": QUERY["findings"], "requested_doc_types": ["pharmacy_bill"],
         "response_text_masked": "Please find the pharmacy bill attached with the stamp.",
         "attached_docs": [{"doc_id": "d9", "doc_type": "pharmacy_bill", "pages": 2, "parse_confidence": 0.95}],
         "code_check": {"requested_present": ["pharmacy_bill"], "requested_missing": [], "unexpected": []}}  # fmt: skip


class OfflineLLM:
    """Deterministic stand-in for the model so the flows can be shown without Ollama. It answers from the FACTS the code
    already computed and never invents evidence; the real agents run with INS_CREW_LLM=ollama."""

    async def complete(self, *, alias: str, messages: list[dict[str, str]], schema: Any, metadata: dict[str, Any], max_tokens: int, timeout: float) -> LLMResult:  # noqa: ASYNC109
        agent, user = metadata.get("agent", ""), messages[-1]["content"]
        keys = list(dict.fromkeys(re.findall(r'"key": "([^"]+)"', user)))
        out: dict[str, Any]
        if agent == "identity":
            out = {"field_observations": [], "reconciliation_notes": "Compared the claim documents with the member record.", "suspected_issue_codes": []}
        elif agent == "authenticity":
            out = {"anomalies": [], "explanations": ["No authenticity signal was raised by the checks."], "suspected_issue_codes": []}
        elif agent == "supervisor":
            issues = re.findall(r"(NAME_MISMATCH|DOB_MISMATCH|POLICY_NO_MISMATCH|MEMBER_NOT_FOUND)", user)
            out = {"summary_for_reviewer": "Identity checks disagree with the member record; a reviewer should confirm the patient." if issues
                   else "All verification steps passed without findings.", "disagreements": [],
                   "recommended_next_action": "manual_review" if issues else "proceed"}  # fmt: skip
        elif agent == "query_drafter":
            out = {"sentences": {k: "Please share the pharmacy bill with the hospital stamp for this admission." for k in keys}, "citations": []}
        elif agent == "coverage":  # claims no clause itself: the agent's code then quotes the wording by keyword (or reports insufficient evidence)
            out = {"applicable_clauses": [], "exclusions_hit": [], "insufficient_evidence": False}
        elif agent == "calc_mapper":  # lines the keyword rules could not place stay "other", as in rules-only mode
            m = re.search(r"LINES:\n(\[.*\])", user)  # one JSON line; CrewAI adds its own text after it
            lines = json.loads(m.group(1)) if m else []
            out = {"lines": [{"line_ref": ln["line_ref"], "mapped_group": "other", "source": "agent", "rationale": "offline stand-in"} for ln in lines]}
        elif agent == "triage":
            out = {"verdict": "resolved", "resolved_finding_keys": keys, "remaining_finding_keys": [], "notes": "The requested document was attached."}
        else:
            out = {"lines": []}
        return LLMResult(json.dumps(out), 0, 0, "offline-stand-in")


def _app() -> Any:
    from .app import create_app

    if os.environ.get("INS_CREW_LLM", "offline") != "ollama":
        return create_app(Settings(crew_allow_dev_token=True), llm=OfflineLLM()), "offline stand-in (no model)"
    model = os.environ.get("INS_CREW_MODEL", "gemma4:latest")
    st = Settings(crew_allow_dev_token=True, alias_smart=model, alias_fast=model, alias_fallback=model, crew_request_timeout=300)
    llm = GatewayLLM(os.environ.get("INS_LLM_GATEWAY_URL", "http://localhost:11434"), "ollama", reasoning_effort="none")
    return create_app(st, llm=llm), f"Ollama {model}"


async def _run() -> None:
    app, mode = _app()
    print(f"insurer crew demo: CrewAI flows, model = {mode}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://crew", headers={"X-Service-Token": "dev"}, timeout=600) as c:
        for title, contexts in CLAIMS.items():
            body = {"request_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "contexts": contexts}
            j = (await c.post("/v1/flows/verification", json=body)).json()
            ident = j.get("outputs", {}).get("identity", {})
            print(f"\n===== VerificationFlow: {title} =====\nsteps: {' -> '.join(j.get('steps', []))}\n"
                  f"identity issues: {ident.get('suspected_issue_codes', ident.get('failure'))}\n"
                  f"supervisor: {j.get('recommended_next_action')} -> outcome: {j.get('outcome')}")  # fmt: skip
        for kind, ctx in (("draft", {"query_drafter": QUERY}), ("triage", {"triage": REPLY})):
            body = {"request_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "contexts": ctx, "kind": kind}
            j = (await c.post("/v1/flows/query", json=body)).json()
            out = j.get("output") or {}
            print(f"\n===== QueryFlow ({kind}) =====\nsteps: {' -> '.join(j.get('steps', []))}")
            print(out.get("text", "")[:600] if kind == "draft" else f"verdict: {out.get('verdict')}, resolved: {out.get('resolved_finding_keys')}")


def kickoff() -> None:
    os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
    asyncio.run(_run())


def plot() -> None:
    from .flows import QueryFlow, VerificationFlow

    for flow, name in ((VerificationFlow(), "insurer_verification_flow.html"), (QueryFlow(), "insurer_query_flow.html")):
        src = Path(flow.plot(name, show=False))
        for f in src.parent.iterdir():  # the page and its js/css, copied next to where the command runs
            shutil.copy(f, Path.cwd() / f.name)
        print(Path.cwd() / name)


if __name__ == "__main__":
    kickoff()
