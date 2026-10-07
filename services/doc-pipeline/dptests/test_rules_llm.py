"""The deterministic extractor against the synthetic corpus (labels come from the renderer, not from the extractor)."""

import json
import re

import pytest
from docpipe.llm import RulesLLM
from docpipe.pipeline import run
from docpipe.settings import Settings
from synth.archetypes import RECIPES
from synth.case import build_case

S = Settings(allow_cloud=False)


def label(meta, field):
    return next((x["value"] for x in meta["labels"] if x["field"] == field), None)


@pytest.mark.parametrize("rid", ["S01", "S02", "S03"])
async def test_rules_extractor_matches_the_labels(rid):
    for idx in range(3):
        b = build_case(11, idx, RECIPES[rid])
        for d in b["case"]["documents"]:
            meta = json.loads(b["files"][d["labels"].replace("\\", "/")])
            r = await run(b["files"][d["clean_file"]], S, RulesLLM())
            assert r["doc_type"] == d["doc_type"], (d["doc_type"], r["doc_type"])
            assert (
                len(r["passes"]) == 2
                and not r["needs_review"]
                or r["doc_type"] in ("discharge_summary", "prescription", "implant_sticker")
            ), r["review_reasons"]
            t = r["passes"][0]["typed_json"]
            if d["doc_type"] == "prescription":
                assert t["medicines"], "completeness requires medicines on a prescription"
            if (name := label(meta, "patient_name")) is not None:
                assert t["patient_name"] == name
            if (tot := label(meta, "total")) is not None:
                assert re.sub(r"[,\s]", "", str(t["total"])) == re.sub(r"[,\s]", "", tot)
            if (dt := label(meta, "date")) is not None:
                assert t["date"] == dt
            if (adm := label(meta, "admitted_on")) is not None:
                assert t["admitted_on"] == adm


async def test_unknown_fields_stay_null_and_two_passes_agree():
    b = build_case(11, 1, RECIPES["S01"])
    r = await run(b["files"]["docs/03_final_bill.pdf"], S, RulesLLM())
    assert [p["pass_no"] for p in r["passes"]] == [1, 2]
    assert r["passes"][0]["typed_json"] == r["passes"][1]["typed_json"]
