"""doc-pipeline output -> hospital-api parse/classify callbacks, replayed the way n8n flow F1 does it, with the
deterministic extractor (no model). Pins the contract that three e2e runs had to discover: two passes are needed or a
document stays 'processing'; the classify body uses doc_type_conf; names come back clean."""

import uuid
from typing import Any

import httpx
import pytest
from docpipe.llm import RulesLLM
from docpipe.pipeline import run
from docpipe.settings import Settings as DocSettings
from synth.archetypes import RECIPES
from synth.case import build_case
from tests.helpers import files, new_case

pytestmark = pytest.mark.integration
DS = DocSettings(allow_cloud=False)


async def upload(c: httpx.AsyncClient, tok: Any, case_id: str, name: str, data: bytes) -> str:
    r = await c.post(
        f"/v1/cases/{case_id}/documents",
        headers=tok("desk1"),
        files=files((name, data, "application/pdf")),
    )
    assert r.status_code == 202, r.text
    return str(r.json()["documents"][0]["id"])


async def replay_f1(
    c: httpx.AsyncClient, tok: Any, doc: str, result: dict[str, Any], passes: int | None = None
) -> dict[str, Any]:
    """The HTTP sequence of flow F1 after the pipeline job succeeded (steps 12-24)."""
    svc = tok("svc:n8n")
    q = {"quality_score": 0.9, "flags": [], "has_required_stamp": True}
    assert (
        await c.post(f"/v1/internal/documents/{doc}/quality", json=q, headers=svc)
    ).status_code == 200
    ps = result["passes"][:passes] if passes else result["passes"]
    for p in ps:  # exactly the pass objects, as flow node 20 posts them
        r = await c.post(f"/v1/internal/documents/{doc}/parse", json=p, headers=svc)
        assert r.status_code == 200, (p.keys(), r.text)
    body = {"doc_type": result["doc_type"], "confidence": result["doc_type_conf"], "source": "auto"}
    r = await c.post(f"/v1/internal/documents/{doc}/classify", json=body, headers=svc)
    assert r.status_code == 200, r.text
    if len(result["passes"][:passes] if passes else result["passes"]) < 2:  # flow nodes 23-24
        await c.post(
            f"/v1/internal/documents/{doc}/status",
            json={"parse_status": "needs_review", "error": "single_pass"},
            headers=svc,
        )
    return (await c.get(f"/v1/documents/{doc}", headers=tok("officer1"))).json()  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "dtype", ["prescription", "pharmacy_bill", "final_bill", "discharge_summary"]
)
async def test_pipeline_output_is_accepted_and_the_document_ends_parsed(
    client: httpx.AsyncClient, tok: Any, dtype: str
) -> None:
    b = build_case(21, 1, RECIPES["S01"])
    d = next(x for x in b["case"]["documents"] if x["doc_type"] == dtype)
    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    doc = await upload(client, tok, case["id"], f"{dtype}.pdf", b["files"][d["clean_file"]])
    result = await run(b["files"][d["clean_file"]], DS, RulesLLM())
    assert [p["pass_no"] for p in result["passes"]] == [1, 2]
    got = await replay_f1(client, tok, doc, result)
    assert got["doc_type"] == dtype and got["parse_status"] == "parsed", got
    assert (
        got["classification_confidence"] >= 0.75
    )  # doc_type_conf reached the API (the flow once read the wrong key)
    parse = (await client.get(f"/v1/documents/{doc}/parse", headers=tok("officer1"))).json()
    names = [
        p["typed_json"].get("patient_name")
        for p in parse["passes"]
        if p["typed_json"].get("patient_name")
    ]
    assert names and all(
        n == b["case"]["member"]["full_name"] for n in names
    )  # real name, not the rest of the line


async def test_a_single_pass_leaves_the_document_processing_until_the_flow_flags_it(
    client: httpx.AsyncClient, tok: Any
) -> None:
    b = build_case(21, 2, RECIPES["S01"])
    d = next(x for x in b["case"]["documents"] if x["doc_type"] == "final_bill")
    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    doc = await upload(client, tok, case["id"], "bill.pdf", b["files"][d["clean_file"]])
    result = await run(b["files"][d["clean_file"]], DS, RulesLLM())
    svc = tok("svc:n8n")
    await client.post(f"/v1/internal/documents/{doc}/parse", json=result["passes"][0], headers=svc)
    one = (await client.get(f"/v1/documents/{doc}", headers=tok("officer1"))).json()
    assert (
        one["parse_status"] == "processing"
    )  # the API waits for the second pass (this is why the pipeline always sends two)
    await client.post(
        f"/v1/internal/documents/{doc}/status",
        json={"parse_status": "needs_review", "error": "single_pass"},
        headers=svc,
    )
    assert (await client.get(f"/v1/documents/{doc}", headers=tok("officer1"))).json()[
        "parse_status"
    ] == "needs_review"


def test_every_field_the_requirements_demand_is_one_the_pipeline_produces() -> None:
    """Completeness blocks a document whose typed JSON lacks a must_have field; the pipeline must be able to supply it.
    (The fast e2e found `medicines` missing from the prescription schema.)"""
    from docpipe.schemas.fields import FIELDS, LINE_DOCS
    from seed.config_payloads import DOC_REQUIREMENTS

    ALIASES = {
        "lines": "lines",
        "stamp": "stamp",
    }  # lines come from the table extractor, not the model
    for rule in DOC_REQUIREMENTS["rules"]:
        dt = rule["doc_type"]
        if dt not in FIELDS:
            continue
        for f in rule.get("must_have_fields", []):
            ok = (
                f in FIELDS[dt]
                or (f == "lines" and dt in LINE_DOCS)
                or f in ALIASES
                and dt in LINE_DOCS
            )
            assert ok, f"rule {rule['id']} needs {f!r} on {dt} but the pipeline never produces it"
