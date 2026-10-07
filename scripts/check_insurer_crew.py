"""Known-answer check of the insurer crew against the REAL local model (Ollama, gemma4:latest by default).

    uv run python scripts/check_insurer_crew.py            # starts the crew on :8610, runs every agent, prints a table

Each case has an expected outcome decided by hand (a matching identity must raise no issue, a name/DOB mismatch must be
flagged, an arithmetic error must be reported, a bill line must map to the right calculator group, and so on). The agents
must also return schema-valid, non-degraded output and must say "insufficient evidence" instead of inventing clauses when
the knowledge base is not available. Exit code is 1 if any check fails. Needs Ollama running; nothing leaves the machine."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("CREW_PORT", "8610"))
MODEL = os.environ.get("INS_CREW_MODEL", "gemma4:latest")
TOKEN = "check-token"
RAG_URL = os.environ.get("INS_RAG_URL", "http://localhost:8400")
RESULTS: list[tuple[str, bool, float, str]] = []


def post(c: httpx.Client, path: str, context: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t = time.time()
    r = c.post(path, json={"request_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "context": context, "options": {}}, timeout=300)
    dt = time.time() - t
    if r.status_code != 200:
        raise AssertionError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json(), dt


def check(name: str, fn: Any, c: httpx.Client) -> None:
    t = time.time()
    try:
        note = fn(c) or ""
        RESULTS.append((name, True, time.time() - t, note))
    except AssertionError as e:
        RESULTS.append((name, False, time.time() - t, str(e)[:300]))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, time.time() - t, f"{type(e).__name__}: {e}"[:300]))
    n, ok, secs, note = RESULTS[-1]
    print(f"  {'PASS' if ok else 'FAIL'}  {n:<52} {secs:5.1f}s  {note}", flush=True)


def clean(out: dict[str, Any]) -> None:
    assert out.get("degraded") is False, f"degraded output ({out.get('warnings')})"
    assert out.get("model_alias") == MODEL, f"served by {out.get('model_alias')!r}, expected {MODEL!r}"
    assert out.get("trace_id"), "no trace id"


MEMBER = {"member_id": "MEM-****0003", "full_name": "Rohan Iyer", "dob": "2016-09-25", "gender": "M", "policy_number": "POL-NIV-2025-000101"}
DOC = {"doc_id": "d1", "doc_type": "id_proof", "pages": 1, "parse_confidence": 0.97}


def identity_match(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/identity/analyze", {
        "member": MEMBER, "patient": {"full_name": "Rohan Iyer", "dob": "2016-09-25", "gender": "M", "policy_number": MEMBER["policy_number"]},
        "deterministic_facts": {"name_score": 1.0, "dob_match": True, "policy_match": True, "member_found": True},
        "docs": [{**DOC, "extract_masked": {"patient_name": "Rohan Iyer", "dob": "2016-09-25"}}],
    })
    clean(out)
    assert not out["suspected_issue_codes"], f"a matching identity must raise nothing, got {out['suspected_issue_codes']}"
    return "no issues"


def identity_mismatch(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/identity/analyze", {
        "member": MEMBER, "patient": {"full_name": "Suresh Verma", "dob": "1979-03-02", "gender": "M", "policy_number": MEMBER["policy_number"]},
        "deterministic_facts": {"name_score": 0.12, "dob_match": False, "policy_match": True, "member_found": True},
        "docs": [{**DOC, "extract_masked": {"patient_name": "Suresh Verma", "dob": "1979-03-02"}}],
    })
    clean(out)
    codes = set(out["suspected_issue_codes"])
    assert codes & {"NAME_MISMATCH", "DOB_MISMATCH"}, f"a different person must be flagged, got {sorted(codes)}"
    return f"flagged {sorted(codes)}"


def authenticity_clean(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/authenticity/analyze", {
        "arithmetic": {"total_mismatch": False, "total_diff": "0.00", "line_mismatches": []}, "duplicates": {"exact": [], "near": []},
        "stamp_reports": [{"doc_id": "d1", "stamp_present": True, "confidence": 0.93}], "vision_reports": [{"doc_id": "d1", "quality": "good"}],
        "docs": [{"doc_id": "d1", "doc_type": "final_bill", "pages": 1, "parse_confidence": 0.97}],
    })
    clean(out)
    bad = [a for a in out["anomalies"] if a["severity"] in ("warning", "blocker")]
    assert not bad, f"a clean bill must not raise warnings, got {[a['code'] for a in bad]}"
    return "no anomalies"


def authenticity_arithmetic(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/authenticity/analyze", {
        "arithmetic": {"total_mismatch": True, "total_diff": "9500.00", "line_mismatches": []}, "duplicates": {"exact": [], "near": []},
        "stamp_reports": [{"doc_id": "d1", "stamp_present": True, "confidence": 0.9}], "vision_reports": [{"doc_id": "d1", "quality": "good"}],
        "docs": [{"doc_id": "d1", "doc_type": "final_bill", "pages": 1, "parse_confidence": 0.97}],
    })
    clean(out)
    assert "ARITHMETIC_ERROR" in out["suspected_issue_codes"], f"printed total 61500 vs lines 52000 must be reported, got {out['suspected_issue_codes']}"
    return "ARITHMETIC_ERROR reported"


def authenticity_duplicate_and_stamp(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/authenticity/analyze", {
        "arithmetic": {"total_mismatch": False, "total_diff": "0.00", "line_mismatches": []},
        "duplicates": {"exact": ["d1"], "near": ["IC-2026-000007"]},
        "stamp_reports": [{"doc_id": "d1", "stamp_present": False, "confidence": 0.05}], "vision_reports": [{"doc_id": "d1", "quality": "good"}],
        "docs": [{"doc_id": "d1", "doc_type": "final_bill", "pages": 1, "parse_confidence": 0.97}],
    })
    clean(out)
    codes = set(out["suspected_issue_codes"])
    assert {"DUPLICATE_BILL", "STAMP_MISSING"} <= codes, f"expected DUPLICATE_BILL and STAMP_MISSING, got {sorted(codes)}"
    return f"{sorted(codes)}"


LINES = [
    ("L1", "Room rent general ward", "room_rent"), ("L2", "ICU charges", "icu"), ("L3", "Surgeon fees", "surgeon_fees"),
    ("L4", "Anaesthesia charges", "anaesthesia"), ("L5", "Inj Ceftriaxone 1g", "medicine"), ("L6", "Disposable syringe 5ml", "consumable"),
    ("L7", "Blood test CBC", "investigation"), ("L8", "OT charges operation theatre", "ot_charges"), ("L9", "Consultation fees", "doctor_fees"),
    ("L10", "Urine bag", "consumable"), ("L11", "Attendant meals and toiletries", "non_medical"), ("L12", "Ambulance transfer", "ambulance"),
]


def calc_map(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/calc/map-lines", {
        "lines": [{"line_ref": r, "category": "other", "description": d, "qty": "1", "unit_price": "1000.00", "amount": "1000.00"} for r, d, _ in LINES],
        "product_code": "HF-GOLD",
    })
    clean(out)
    got = {m["line_ref"]: m["mapped_group"] for m in out["lines"]}
    wrong = {r: (got.get(r), want) for r, _, want in LINES if got.get(r) != want}
    acc = 1 - len(wrong) / len(LINES)
    assert acc >= 0.8, f"line mapping accuracy {acc:.0%}; wrong (got, expected): {wrong}"
    return f"{acc:.0%} correct" + (f"; wrong {wrong}" if wrong else "")


def coverage_without_kb(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/coverage/analyze", {
        "policy": {"product_code": "HF-GOLD", "effective_date": "2026-01-01"}, "diagnoses": [{"icd": "K35.80", "name": "Acute appendicitis"}],
        "procedures": [{"code": "0DTJ0ZZ", "name": "Resection of appendix"}], "diagnosis_codes": ["K35.80"], "procedure_codes": ["0DTJ0ZZ"],
        "claim_type": "cashless", "admission_type": "emergency", "admitted_on": "2026-08-31",
    })
    clean(out) if out.get("model_alias") else None
    assert not out.get("citations"), "no knowledge base: the agent must not cite clauses"
    assert out.get("insufficient_evidence") is True, "without the knowledge base the agent must say it has insufficient evidence"
    return "refused to invent clauses"


def coverage_with_kb(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/coverage/analyze", {
        "policy": {"product_code": "HEALTH-BASIC", "effective_date": "2026-01-01"}, "diagnoses": [{"icd": "K80.20", "name": "Calculus of gallbladder"}],
        "procedures": [{"code": "0FT44ZZ", "name": "Cholecystectomy"}], "diagnosis_codes": ["K80.20"], "procedure_codes": ["Cholecystectomy"],
        "claim_type": "cashless", "admission_type": "planned", "admitted_on": "2026-09-02",
    })
    clean(out)
    assert "rag_unavailable" not in out.get("warnings", []), f"knowledge base not reachable: {out.get('warnings')}"
    assert out.get("insufficient_evidence") is False, f"the KB documents this procedure but the agent says insufficient evidence; warnings {out.get('warnings')}, clauses {out.get('applicable_clauses')}"
    clauses = out.get("applicable_clauses") or []
    assert clauses, "no clause returned although the knowledge base holds the product wording"
    cites = out.get("citations") or []
    assert cites, "clauses must carry citations"
    bad = [x for x in cites if not str(x.get("chunk_id", "")).startswith("pw-HPA-v3")]
    assert not bad, f"citations must come from the product's current wording (pw-HPA-v3), got {[x.get('chunk_id') for x in bad]}"
    return f"{len(clauses)} clause(s), cited {sorted({x['chunk_id'] for x in cites})[:2]}"


def query_draft(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/query/draft", {
        "round": 1, "claim_ref": "HC-2026-000123", "hospital_name": "City Care Hospital",
        "findings": [
            {"key": "f1", "kind": "completeness.missing_required", "severity": "blocker", "detail": "pharmacy_bill", "requested_doc_type": "pharmacy_bill"},
            {"key": "f2", "kind": "auth.arithmetic_error", "severity": "warning", "detail": "final bill total differs from the sum of lines by 9500.00"},
        ],
        "requirements": [{"doc_type": "pharmacy_bill", "rule": "required for every claim"}],
    })
    clean(out)
    text = out["text"].lower()
    assert "pharmacy" in text, "the draft must ask for the pharmacy bill"
    assert out["tone_check"]["polite"] and out["tone_check"]["no_accusation"] and out["tone_check"]["no_promise"], out["tone_check"]
    assert not any(w in text for w in ("fraud", "fake", "forged", "will be approved", "guarantee")), "accusatory or promising language"
    assert set(out["finding_keys"]) >= {"f1"}, f"finding keys {out['finding_keys']}"
    assert "pharmacy_bill" in out["requested_doc_types"], out["requested_doc_types"]
    return f"{len(out['text'])} chars, polite, asks for pharmacy_bill"


def triage_resolved(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/query/triage", {
        "open_findings": [{"key": "f1", "kind": "completeness.missing_required", "severity": "blocker", "detail": "pharmacy_bill"}],
        "requested_doc_types": ["pharmacy_bill"], "response_text_masked": "Please find the pharmacy bill attached with the stamp.",
        "attached_docs": [{"doc_id": "d9", "doc_type": "pharmacy_bill", "pages": 2, "parse_confidence": 0.95, "extract_masked": {"total": "4200.00"}}],
        "code_check": {"requested_present": ["pharmacy_bill"], "requested_missing": [], "unexpected": []},
    })
    clean(out)
    assert out["verdict"] in ("resolved", "partially_resolved"), f"document supplied, verdict {out['verdict']}"
    assert "f1" in out["resolved_finding_keys"] or out["verdict"] == "resolved", out
    return out["verdict"]


def triage_unresolved(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/query/triage", {
        "open_findings": [{"key": "f1", "kind": "completeness.missing_required", "severity": "blocker", "detail": "pharmacy_bill"}],
        "requested_doc_types": ["pharmacy_bill"], "response_text_masked": "We are checking and will revert shortly. Thank you for your patience.",
        "attached_docs": [], "code_check": {"requested_present": [], "requested_missing": ["pharmacy_bill"], "unexpected": []},
    })
    clean(out)
    assert out["verdict"] in ("unresolved", "off_topic"), f"nothing supplied, verdict {out['verdict']}"
    assert "f1" in out["remaining_finding_keys"], out["remaining_finding_keys"]
    return out["verdict"]


def supervisor(c: httpx.Client) -> str:
    out, _ = post(c, "/v1/supervisor/summarize", {"step_outputs": {
        "identity": {"suspected_issue_codes": ["NAME_MISMATCH", "DOB_MISMATCH"], "reconciliation_notes": "ID proof shows a different person"},
        "authenticity": {"suspected_issue_codes": [], "explanations": ["document looks genuine"]},
        "coverage": {"applicable_clauses": [], "insufficient_evidence": True},
    }})
    clean(out)
    assert out["recommended_next_action"] in ("manual_review", "needs_info", "escalate"), f"identity conflict yet next action is {out['recommended_next_action']}"
    assert out["summary_for_reviewer"].strip(), "empty summary"
    return f"next action {out['recommended_next_action']}"


def verification_flow(c: httpx.Client) -> str:
    """The CrewAI VerificationFlow end to end: a different person must not reach the decision gate unreviewed."""
    patient = {"full_name": "Suresh Verma", "dob": "1979-03-02", "gender": "M", "policy_number": MEMBER["policy_number"]}
    contexts = {
        "identity": {"member": MEMBER, "patient": patient, "deterministic_facts": {"name_score": 0.12, "dob_match": False, "policy_match": True, "member_found": True},
                     "docs": [{**DOC, "extract_masked": {"patient_name": "Suresh Verma", "dob": "1979-03-02"}}]},
        "calc_mapper": {"lines": [{"line_ref": "L1", "category": "room", "description": "Room rent general ward", "qty": "1", "unit_price": "1000.00", "amount": "1000.00"}],
                        "product_code": "HF-GOLD"},
    }
    r = c.post("/v1/flows/verification", json={"request_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "contexts": contexts}, timeout=600)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    assert j["steps"] == ["identity", "calc_mapper", "supervisor"], j["steps"]
    for name in j["steps"]:
        assert "failure" not in j["outputs"][name], f"{name} failed: {j['outputs'][name]}"
        clean(j["outputs"][name])
    assert j["outcome"] == "review", f"identity conflict yet outcome {j['outcome']} ({j['recommended_next_action']})"
    return f"{' -> '.join(j['steps'])} -> {j['outcome']}"


def rag_token() -> str | None:
    """A crew service token for the local RAG service, or None when RAG is not running (the no-KB check runs instead)."""
    try:
        httpx.get(f"{RAG_URL}/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        return None
    secret = os.environ.get("RAG_JWT_SECRET")
    if not secret:
        for ln in (ROOT / ".env").read_text().splitlines():
            if ln.startswith("RAG_JWT_SECRET="):
                secret = ln.split("=", 1)[1].strip()
    if not secret:
        return None
    sys.path.insert(0, str(ROOT / "services/rag-service"))
    from rag_service.security import issue_token

    return issue_token(secret, "insurer-crew")


def main() -> int:
    token = rag_token()
    only = os.environ.get("CHECK_ONLY", "")  # run just the checks whose name contains this
    env = {**os.environ, "INS_LLM_GATEWAY_URL": "http://localhost:11434", "INS_LLM_VIRTUAL_KEY": "ollama", "INS_ALIAS_SMART": MODEL, "INS_ALIAS_FAST": MODEL,
           "INS_ALIAS_FALLBACK": MODEL, "INS_LLM_REASONING_EFFORT": "none", "INS_CREW_SERVICE_TOKENS": TOKEN, "INS_CREW_REQUEST_TIMEOUT": "240",
           "INS_RAG_URL": RAG_URL if token else "", "INS_RAG_TOKEN": token or ""}
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=3).raise_for_status()
    except httpx.HTTPError:
        print("Ollama is not reachable on :11434")
        return 2
    log = (ROOT / ".e2e-logs").joinpath("check-insurer-crew.log").open("wb")
    p = subprocess.Popen(["uv", "run", "uvicorn", "insurer_crew.main:app", "--port", str(PORT), "--log-level", "warning"], cwd=ROOT / "insurer/crew", env=env,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)  # noqa: S603, S607
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://localhost:{PORT}/v1/health", timeout=2).status_code < 500:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        c = httpx.Client(base_url=f"http://localhost:{PORT}", headers={"X-Service-Token": TOKEN})
        print(f"insurer crew on the real model {MODEL}, knowledge base {'ON' if token else 'off (start it with make run-rag)'}:")
        for name, fn in [
            ("identity: matching person raises nothing", identity_match), ("identity: different person is flagged", identity_mismatch),
            ("authenticity: clean bill raises nothing", authenticity_clean), ("authenticity: total mismatch is reported", authenticity_arithmetic),
            ("authenticity: duplicate bill and missing stamp", authenticity_duplicate_and_stamp), ("calc map-lines: 12 lines to calculator groups", calc_map),
            ("coverage: with the knowledge base -> cited clauses", coverage_with_kb) if token else ("coverage: no knowledge base -> no invented clauses", coverage_without_kb), ("query draft: polite, specific, no promises", query_draft),
            ("triage: supplied document resolves the finding", triage_resolved), ("triage: a stalling reply stays unresolved", triage_unresolved),
            ("supervisor: identity conflict is not waved through", supervisor),
            ("CrewAI VerificationFlow: mismatch goes to human review", verification_flow),
        ]:
            if only and only not in name:
                continue
            check(name, fn, c)
    finally:
        os.killpg(p.pid, signal.SIGTERM)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; total {sum(r[2] for r in RESULTS):.0f}s")
    (ROOT / ".e2e-logs" / "check-insurer-crew.json").write_text(json.dumps([{"check": n, "ok": ok, "seconds": round(s, 1), "note": note} for n, ok, s, note in RESULTS], indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
