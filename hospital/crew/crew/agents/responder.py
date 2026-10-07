"""Query triage and grounded drafting (doc 09 task 7)."""

from __future__ import annotations

from typing import Any

from crew import prompts
from crew.guards import grounding, supervisor
from crew.llm import LLM
from crew.settings import Settings

CATEGORY_DEFAULT = {
    "missing_document": {"action": "send_documents", "needs_docs": True, "escalation_risk": False},
    "illegible_document": {
        "action": "send_documents",
        "needs_docs": True,
        "escalation_risk": False,
    },
    "identity_mismatch": {"action": "clarify", "needs_docs": False, "escalation_risk": True},
    "policy_exclusion": {"action": "clarify", "needs_docs": False, "escalation_risk": True},
}


async def triage(q: dict[str, Any], llm: LLM, st: Settings) -> dict[str, Any]:
    base = CATEGORY_DEFAULT.get(
        q["category"], {"action": "clarify", "needs_docs": False, "escalation_risk": False}
    )
    p = prompts.load(st.prompt_dir, "triage", st.prompt_pins)
    resp, mi = await llm.complete_json(
        model=st.model_general,
        prompt=p.render(text=q["text"], category=q["category"], round=str(q["round"])),
    )
    action = (
        resp.get("action")
        if resp.get("action") in ("send_documents", "clarify")
        else base["action"]
    )
    out = {
        "action": action,
        "needs_docs": bool(resp.get("needs_docs", base["needs_docs"])),
        # the model may raise the flag but never lower a rule-based one
        "escalation_risk": bool(resp.get("escalation_risk")) or base["escalation_risk"],
        "note": str(resp.get("note", ""))[:300] or None,
    }
    return out | {"_model_info": {"alias": mi["model"], "prompt_version": p.version}}


def sources_text(ctx: dict[str, Any]) -> tuple[str, str]:
    """(numbered sources shown to the model, evidence text the guard checks quotes against)."""
    ev = ctx["evidence"]
    numbered = "\n".join(f"S{i + 1}: {s}" for i, s in enumerate(ev))
    return numbered, "\n".join(ev)


async def draft(ctx: dict[str, Any], llm: LLM, st: Settings) -> dict[str, Any]:
    numbered, evidence = sources_text(ctx)
    p = prompts.load(st.prompt_dir, "draft_reply", st.prompt_pins)
    feedback = ""
    last: dict[str, Any] = {}
    mi: dict[str, Any] = {}
    for _ in range(2):  # one regeneration with the guard's complaints fed back
        resp, mi = await llm.complete_json(
            model=st.model_general, prompt=p.render(sources=numbered, feedback=feedback)
        )
        text = str(resp.get("draft_text", "")).strip()
        cites = [c for c in resp.get("citations", []) if isinstance(c, dict)]
        problems = grounding.check(text, cites, evidence)
        last = {"draft_text": text, "citations": cites, "missing": list(resp.get("missing", []))}
        if not problems:
            break
        feedback = (
            "Your previous answer had problems: "
            + "; ".join(x["detail"] for x in problems)
            + ". Fix them."
        )
    sup = supervisor.verdict([last["draft_text"]])
    return {**last, "model_info": {"alias": mi["model"], "prompt_version": p.version}, "supervisor": sup,
            "unsupported": grounding.check(last["draft_text"], last["citations"], evidence)}  # fmt: skip
