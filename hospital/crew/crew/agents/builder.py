"""Claim builder and repair (doc 09 tasks 5-6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from claim_contract.enums import BillCategory

from crew import prompts
from crew.llm import LLM
from crew.settings import Settings
from crew.tools import assemble as asm

CATS = {c.value for c in BillCategory}
RULES = yaml.safe_load((Path(__file__).parent.parent / "tools" / "repair_paths.yaml").read_text())


async def build(ctx: dict[str, Any], llm: LLM, st: Settings) -> dict[str, Any]:
    out = asm.assemble(ctx)
    info: dict[str, Any] = {"alias": "deterministic", "prompt_version": None}
    if out["ambiguous"]:
        p = prompts.load(st.prompt_dir, "map_category", st.prompt_pins)
        lines = "\n".join(f"{i}: {out['lines'][i]}" for i in out["ambiguous"])
        resp, mi = await llm.complete_json(model=st.model_local, prompt=p.render(lines=lines))
        for it in resp.get("items", []):
            i, cat = it.get("index"), it.get("category")
            if i in out["ambiguous"] and cat in CATS:  # unknown labels are ignored, never trusted
                out["payload"]["bill_lines"][i]["category"] = cat
        info = {
            "alias": mi["model"],
            "prompt_version": p.version,
            "tokens_in": mi["tokens_in"],
            "tokens_out": mi["tokens_out"],
        }
    return {"payload": out["payload"], "provenance": out["provenance"], "model_info": info}


class RepairRejected(Exception):
    pass


def check_repair(before: dict[str, Any], after: dict[str, Any]) -> None:
    allowed = RULES["allowed"] + RULES["deterministic"]
    for path in asm.diff_paths(before, after):
        if path not in allowed and path != "notes":
            raise RepairRejected(f"change outside the whitelist: {path}")


async def repair(
    payload: dict[str, Any], errors: list[dict[str, Any]], llm: LLM, st: Settings
) -> dict[str, Any]:
    """Deterministic fix first (recompute totals); a model only maps categories; anything else is rejected."""
    fixed = asm.recompute_totals(payload)
    info: dict[str, Any] = {"alias": "deterministic", "prompt_version": None}
    bad = [
        e for e in errors if "category" in (e.get("field") or "") or e.get("code") == "V_CATEGORY"
    ]
    if bad:
        p = prompts.load(st.prompt_dir, "map_category", st.prompt_pins)
        idx = [i for i, ln in enumerate(fixed["bill_lines"]) if ln["category"] == "other"]
        lines = "\n".join(f"{i}: {fixed['bill_lines'][i]['description']}" for i in idx)
        resp, mi = await llm.complete_json(model=st.model_local, prompt=p.render(lines=lines))
        for it in resp.get("items", []):
            if it.get("index") in idx and it.get("category") in CATS:
                fixed["bill_lines"][it["index"]]["category"] = it["category"]
        info = {"alias": mi["model"], "prompt_version": p.version}
    check_repair(payload, fixed)
    return {"payload": fixed, "provenance": {}, "model_info": info}
