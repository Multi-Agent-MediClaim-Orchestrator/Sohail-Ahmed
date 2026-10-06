"""Builders for claim-flow tests: a ready case, a draft payload, and a hospital app wired to insurer-sim."""

import uuid
from typing import Any

import httpx
from tests.helpers import new_case
from tests.test_completeness_api import add_doc


async def ready_case(
    c: httpx.AsyncClient, tok: Any, pharmacy_total: str = "1000.00", **over: Any
) -> dict[str, Any]:
    """A case whose three required documents are parsed and complete (status docs_complete)."""
    case = await new_case(c, tok("officer1"), "UH-" + uuid.uuid4().hex[:8], **over)
    docs = {}
    for t in ("prescription", "pharmacy_bill", "final_bill"):
        typed = None
        if t == "pharmacy_bill":
            typed = {
                "patient_name": "Ravi Kumar",
                "date": "2026-09-29",
                "total": pharmacy_total,
                "lines": [{"amount": pharmacy_total}],
                "medicines": ["x"],
            }
        docs[t] = await add_doc(c, tok, case["id"], t, typed=typed)
    r = await c.get(f"/v1/cases/{case['id']}", headers=tok("officer1"))
    assert r.json()["status"] == "docs_complete", r.json()
    return {
        "id": case["id"],
        "claim_ref": case["claim_ref"],
        "docs": docs,
        "admitted_on": over.get("admitted_on", "2026-09-28"),
        "discharged_on": over.get("discharged_on", "2026-10-02"),
    }


def draft_payload(
    case: dict[str, Any], *, v01_error: bool = False, duplicate: bool = False
) -> dict[str, Any]:
    docs = case["docs"]
    lines = [
        {
            "line_no": 1,
            "code": "PH-1",
            "description": "Pharmacy items",
            "category": "medicine",
            "qty": "1",
            "unit_price": "1000.00",
            "amount": "1000.00",
            "service_date": case["admitted_on"],
            "source_doc_id": docs["pharmacy_bill"],
            "source_page": 1,
        },
        {
            "line_no": 2,
            "code": "RM-1",
            "description": "Room rent",
            "category": "room",
            "qty": "1",
            "unit_price": "1000.00",
            "amount": "1000.00",
            "service_date": case["admitted_on"],
            "source_doc_id": docs["final_bill"],
            "source_page": 1,
        },
    ]
    if duplicate:
        lines.append(dict(lines[0], line_no=3))
    gross = "3000.00" if duplicate else "2000.00"
    if v01_error:
        gross = "2500.00"
    return {
        "patient": {
            "full_name": "Ravi Kumar",
            "dob": "1984-03-12",
            "gender": "M",
            "member_id": "M-77123",
            "policy_number": "AH-993201",
        },
        "admission": {
            "admission_type": "planned",
            "admitted_on": case["admitted_on"],
            "discharged_on": case["discharged_on"],
            "diagnosis_codes": ["K80.2"],
            "procedure_codes": [],
            "treating_doctor": "Dr. Rao",
        },
        "bill_lines": lines,
        "totals": {"gross": gross, "discounts": "0.00", "claimed": gross},
        "documents": [docs["prescription"], docs["pharmacy_bill"], docs["final_bill"]],
    }


def crew_body(case: dict[str, Any], job_id: str, round_: int = 0, **kw: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "payload": draft_payload(case, **kw),
        "repair_round": round_,
        "provenance": {
            "patient.dob": {"doc_id": case["docs"]["prescription"], "page": 1, "confidence": 0.97}
        },
        "model_info": {"alias": "extract-main", "prompt_version": "cb-v4", "trace_id": "t-1"},
    }
