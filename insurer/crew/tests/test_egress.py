"""SEC-T3 (insurer side): whatever reaches the LLM gateway from insurer-crew contains no raw identifiers, for contexts that carry PII."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from crew_helpers import ScriptedLLM, body, make_client

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "infra" / "llm-gateway"))
from callbacks.core import flatten, pii_hits  # noqa: E402

PII = ["Aadhaar 2345 6789 0123", "PAN ABCDE1234F", "call 9876543210", "mail a.b@example.com", "MEM-12345678"]

CTX = {
    "/v1/query/draft": lambda pii: {"round": 1, "hospital_name": "Sunrise Hospital", "findings": [{"key": "F1", "kind": "missing_document", "severity": "blocker", "detail": f"Document missing {pii}"}]},
    "/v1/query/triage": lambda pii: {"open_findings": [{"key": "F1", "kind": "x"}], "response_text_masked": f"Reply {pii}", "attached_docs": [], "code_check": {}},
    "/v1/supervisor/summarize": lambda pii: {"step_outputs": {"identity": {"note": f"seen {pii}"}}},
    "/v1/identity/analyze": lambda pii: {"member": {"full_name": "Ravi Kumar"}, "patient": {"full_name": "Ravi Kumar"}, "docs": [{"doc_id": "d1", "text_excerpt_masked": pii}]},
    "/v1/authenticity/analyze": lambda pii: {"docs": [{"doc_id": "d1", "text_excerpt_masked": pii}], "bill_lines": []},
}


@pytest.mark.parametrize("path", sorted(CTX))
@pytest.mark.parametrize("pii", PII)
async def test_pii_never_leaves_the_crew(settings, path, pii):
    llm = ScriptedLLM(lambda *a: {"sentences": {"F1": "Please upload the document."}, "verdict": "unresolved", "resolved_finding_keys": [], "remaining_finding_keys": ["F1"], "notes": "n",
                                  "summary_for_reviewer": "ok", "disagreements": [], "recommended_next_action": "manual_review", "field_observations": [], "reconciliation_notes": "", "suspected_issue_codes": [],
                                  "anomalies": [], "explanations": []})
    c, _ = make_client(llm, settings)
    r = await c.post(path, json=body(CTX[path](pii)))
    assert r.status_code == 400 and r.json()["code"] == "pii_in_context", (path, pii, r.status_code)  # blocked at the door
    assert llm.calls == []  # nothing was sent upstream
    for call in llm.calls:
        assert not pii_hits(flatten({"messages": call["messages"]}))


async def test_clean_contexts_produce_prompts_the_gateway_guard_accepts(settings):
    llm = ScriptedLLM(lambda *a: {"sentences": {"F1": "Please upload the document."}})
    c, _ = make_client(llm, settings)
    r = await c.post("/v1/query/draft", json=body(CTX["/v1/query/draft"]("[PERSON_1] raised it")))
    assert r.status_code == 200 and llm.calls
    for call in llm.calls:
        assert pii_hits(flatten({"messages": call["messages"]})) == set()
