"""Query triage and grounded drafting (doc 09 task 7) by two CrewAI agents. The grounding rules (G01-G08) are the draft
task's guardrail: an ungrounded draft goes back to the agent once with the complaints; the supervisor checklist runs last."""

from __future__ import annotations

from typing import Any

from crew import prompts, team
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
    res = await team.run_task(
        "query_triage",
        p.render(text=q["text"], category=q["category"], round=str(q["round"])),
        llm,
        st.model_general,
    )
    resp, mi = res.data, res.model_info
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


def _cites(resp: dict[str, Any]) -> list[dict[str, Any]]:
    c = resp.get("citations")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def sources_text(ctx: dict[str, Any]) -> tuple[str, str]:
    """(numbered sources shown to the model, evidence text the guard checks quotes against)."""
    ev = ctx["evidence"]
    numbered = "\n".join(f"S{i + 1}: {s}" for i, s in enumerate(ev))
    return numbered, "\n".join(ev)


async def draft(ctx: dict[str, Any], llm: LLM, st: Settings) -> dict[str, Any]:
    numbered, evidence = sources_text(ctx)
    p = prompts.load(st.prompt_dir, "draft_reply", st.prompt_pins)

    def grounded(resp: dict[str, Any], attempt: int) -> str | None:
        problems = grounding.check(str(resp.get("draft_text", "")).strip(), _cites(resp), evidence)
        if not problems or attempt >= 2:  # one regeneration; a draft that stays bad is returned flagged
            return None
        return "Your previous answer had problems: " + "; ".join(x["detail"] for x in problems) + ". Fix them."

    res = await team.run_task(
        "query_responder", p.render(sources=numbered, feedback=""), llm, st.model_general,
        guardrail=grounded, retries=1,
    )  # fmt: skip
    resp, mi = res.data, res.model_info
    last = {"draft_text": str(resp.get("draft_text", "")).strip(), "citations": _cites(resp),
            "missing": list(resp.get("missing", []) or [])}  # fmt: skip
    sup = supervisor.verdict([last["draft_text"]])
    return {**last, "model_info": {"alias": mi["model"], "prompt_version": p.version}, "supervisor": sup,
            "unsupported": grounding.check(last["draft_text"], last["citations"], evidence)}  # fmt: skip
