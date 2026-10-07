"""Admissible-amount estimate before submission (problem statement: the hospital interprets the policy's sub-limits,
co-pays and non-payables before it submits). Facts first, model second, code last:

1. policy terms on the patient's policy card (product, sum insured, validity), read by the doc-pipeline;
2. the product's policy wording from the hospital's own knowledge base (rag-service `hosp_insurer_rules`);
3. the CrewAI policy estimator agent reads the wording and reports room rent %, ICU %, co-pay % and the procedure
   sub-limit with verbatim quotes (guardrail: each quote is in the wording and contains its number); a code parser of
   the same wording overrides any number it can read itself;
4. the insurer's own calculation engine (calc_engine, used as a library) computes the estimate.

The estimate is advice for the billing officer, never a gate: anything missing gives ``status="unavailable"`` with the
reason, and no failure here stops the claim build."""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx

from crew import prompts, team
from crew.guards.pii import PiiDetected
from crew.llm import LLM, LLMUnavailable, SchemaInvalid
from crew.settings import Settings
from crew.tools.numbers import norm_text, parse_amount, parse_date

COLLECTION = "hosp_insurer_rules"
TERMS = ("room_rent_percent", "icu_percent", "co_pay_percent", "sub_limit")
# (ICD-10 prefixes, words in the bill or diagnosis) -> (row name in the wording's sub-limit table, calc-engine procedure group)
PROCEDURES: list[tuple[tuple[str, ...], tuple[str, ...], str, str]] = [
    (("H25", "H26"), ("cataract",), "Cataract surgery", "cataract"),
    (
        ("M17",),
        ("knee replacement", "knee_replacement", "arthroplasty"),
        "Total knee replacement",
        "knee_replacement",
    ),
    (("K40", "K41", "K42", "K43", "K44", "K45", "K46"), ("hernia",), "Hernia repair", "hernia"),
    (("K80", "K81"), ("cholecystectomy",), "Cholecystectomy", "cholecystectomy"),
    (("N85",), ("hysterectomy",), "Hysterectomy", "hysterectomy"),
    (("N20",), ("kidney stone", "lithotripsy", "pcnl"), "Kidney stone removal", "kidney_stone"),
]
PROCEDURE_LINE_GROUPS = {
    "surgeon_fees",
    "ot_charges",
    "anaesthesia",
    "procedure_package",
    "implant",
}


class Rag(Protocol):
    async def search(
        self, collection: str, query: str, filters: dict[str, Any], top_k: int = 6
    ) -> list[dict[str, Any]]: ...


class RagUnavailable(Exception):
    pass


class HttpRag:
    """Read-only client for rag-service ``/v1/search`` with the hospital-crew service token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def search(
        self, collection: str, query: str, filters: dict[str, Any], top_k: int = 6
    ) -> list[dict[str, Any]]:
        body = {
            "collection": collection,
            "query": query,
            "filters": filters,
            "top_k": top_k,
            "rerank": True,
            "metadata": {"agent": "policy_estimator"},
        }
        try:
            r = await self.http.post("/v1/search", json=body)
        except httpx.HTTPError as e:
            raise RagUnavailable(type(e).__name__) from e
        if r.status_code >= 400:
            raise RagUnavailable(f"rag-service {r.status_code}")
        return list(r.json().get("results", []))


# ---------------------------------------------------------------------------------------------- facts from the case
def policy_from_card(ctx: dict[str, Any]) -> dict[str, Any]:
    card = next(
        (
            d.get("typed") or {}
            for d in ctx.get("documents", [])
            if d.get("doc_type") == "policy_card"
        ),
        {},
    )
    product = str(card.get("product_name") or "").strip().upper() or None
    return {"product_code": product, "sum_insured": parse_amount(card.get("sum_insured")),
            "valid_from": parse_date(card.get("valid_from")), "valid_to": parse_date(card.get("valid_to"))}  # fmt: skip


def procedure_of(payload: dict[str, Any]) -> tuple[str, str] | None:
    adm = payload.get("admission", {})
    codes = [
        str(c).upper()
        for c in (adm.get("diagnosis_codes") or []) + (adm.get("procedure_codes") or [])
    ]
    text = " ".join(str(ln.get("description", "")) for ln in payload.get("bill_lines", [])).lower()
    for prefixes, words, row, group in PROCEDURES:
        if any(c.startswith(prefixes) for c in codes) or any(w in text for w in words):
            return row, group
    return None


# ---------------------------------------------------------------------------------------------- wording -> terms (code)
def _pct(rx: str, text: str) -> tuple[str, str] | None:
    m = re.search(rx, text, re.I)
    return (m.group(1), m.group(0)) if m else None


def parse_terms(
    chunks: list[dict[str, Any]], procedure_row: str | None, sum_insured: Decimal
) -> dict[str, dict[str, str]]:
    """Terms the code can read from the wording itself: {term: {"value", "quote", "source_id"}}."""
    out: dict[str, dict[str, str]] = {}
    for i, ch in enumerate(chunks, 1):
        t, sid = ch.get("text", ""), f"W{i}"
        for term, rx in (("room_rent_percent", r"Room rent is limited to ([\d.]+)% of the sum insured"),
                         ("icu_percent", r"ICU charges are limited to ([\d.]+)% of the sum insured"),
                         ("co_pay_percent", r"A co-payment of ([\d.]+)% applies")):  # fmt: skip
            if term not in out and (hit := _pct(rx, t)):
                out[term] = {"value": hit[0], "quote": hit[1], "source_id": sid}
        if "co_pay_percent" not in out and (
            m := re.search(r"No co-payment applies[^.\n]*", t, re.I)
        ):
            out["co_pay_percent"] = {"value": "0", "quote": m.group(0), "source_id": sid}
        if procedure_row and "sub_limit" not in out:
            hit = _sub_limit(t, procedure_row, sum_insured)
            if hit:
                out["sub_limit"] = {"value": hit[0], "quote": hit[1], "source_id": sid}
    return out


def _lakh(cell: str) -> Decimal | None:
    m = re.match(r"\s*([\d.]+)\s*lakh", cell, re.I)
    return Decimal(m.group(1)) * 100000 if m else parse_amount(cell)


def _sub_limit(text: str, row: str, si: Decimal) -> tuple[str, str] | None:
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    header = next((ln for ln in lines if "lakh" in ln.lower()), None)
    target = next((ln for ln in lines if ln.lower().startswith(f"| {row.lower()} |")), None)
    if not header or not target:
        return None
    cols = [c.strip() for c in header.strip().strip("|").split("|")]
    vals = [c.strip() for c in target.strip().strip("|").split("|")]
    for name, val in zip(cols[1:], vals[1:], strict=False):
        if _lakh(name) == si and (amt := parse_amount(val)) is not None:
            return str(amt), target.strip()
    return None


# ---------------------------------------------------------------------------------------------- the agent
def _cited(
    resp: dict[str, Any], chunks: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Terms the agent backed with a verbatim quote containing the number; everything else is dropped with a reason."""
    srcs = {f"W{i}": norm_text(c.get("text", "")) for i, c in enumerate(chunks, 1)}
    cites = {c.get("term"): c for c in resp.get("citations", []) if isinstance(c, dict)}
    ok: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for term in TERMS:
        val = resp.get(term)
        if val in (None, "", "null"):
            continue
        num = parse_amount(val)
        c = cites.get(term) or {}
        quote, sid = str(c.get("quote") or ""), str(c.get("source_id") or "")
        if num is None:
            problems.append(f"{term}: {val!r} is not a number")
        elif not quote or norm_text(quote) not in srcs.get(sid, ""):
            problems.append(f"{term}: the quote is not copied exactly from {sid or 'a source'}")
        elif not any(parse_amount(x) == num for x in re.findall(r"\d[\d,]*(?:\.\d+)?", quote)):
            problems.append(f"{term}: the quote does not contain {val}")
        else:
            ok[term] = {"value": str(num), "quote": quote, "source_id": sid}
    return ok, problems


async def read_terms(chunks: list[dict[str, Any]], product: str, si: Decimal, procedure_row: str | None, llm: LLM,
                     st: Settings) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[str]]:  # fmt: skip
    p = prompts.load(st.prompt_dir, "policy_terms", st.prompt_pins)
    wording = "\n\n".join(f"W{i}: {c.get('text', '')}" for i, c in enumerate(chunks, 1))
    prompt = p.render(
        product=product, sum_insured=str(si), procedure=procedure_row or "none", wording=wording
    )

    def grounded(resp: dict[str, Any], attempt: int) -> str | None:
        _, problems = _cited(resp, chunks)
        if not problems or attempt >= 2:
            return None
        return (
            "Fix these terms: "
            + "; ".join(problems)
            + ". Use null when the wording does not state a term."
        )

    warnings: list[str] = []
    info: dict[str, Any] = {"alias": "deterministic", "prompt_version": p.version}
    agent_terms: dict[str, dict[str, str]] = {}
    try:
        res = await team.run_task(
            "policy_estimator", prompt, llm, st.model_general, guardrail=grounded, retries=1
        )
        agent_terms, dropped = _cited(res.data, chunks)
        warnings += [f"agent_term_dropped:{d.split(':')[0]}" for d in dropped]
        info = {"alias": res.model_info.get("model"), "prompt_version": p.version}
    except (LLMUnavailable, SchemaInvalid, PiiDetected) as e:
        warnings.append(f"agent_unavailable:{type(e).__name__}")
    code_terms = parse_terms(chunks, procedure_row, si)
    terms: dict[str, dict[str, Any]] = {}
    for term in TERMS:
        a, c = agent_terms.get(term), code_terms.get(term)
        if c and a and Decimal(a["value"]) != Decimal(c["value"]):
            warnings.append(f"term_overridden_by_code:{term}")
        if c:
            terms[term] = {**c, "read_by": "code" if not a else "agent+code"}
        elif a:
            terms[term] = {**a, "read_by": "agent"}
    for t in terms.values():
        t["citation"] = chunks[int(t["source_id"][1:]) - 1].get("citation_id")
    return terms, info, warnings


# ---------------------------------------------------------------------------------------------- calculation
def calc_input(payload: dict[str, Any], pol: dict[str, Any], terms: dict[str, dict[str, Any]], group: str | None,
               claim_type: str) -> Any:  # fmt: skip
    from calc_engine.mapping import map_line
    from calc_engine.models import CalcInput, MappedGroup
    from calc_engine.rules_schema import PolicyRules

    adm, pat = payload["admission"], payload["patient"]
    admitted, discharged = (
        date.fromisoformat(adm["admitted_on"]),
        date.fromisoformat(adm["discharged_on"]),
    )
    start = pol["valid_from"] or admitted - timedelta(days=365)
    end = pol["valid_to"] or start + timedelta(days=364)
    lines = []
    for i, ln in enumerate(payload["bill_lines"], 1):
        m = map_line(ln.get("category", "other"), ln.get("description", ""))
        g = m.mapped_group if m else MappedGroup.other
        lines.append({"line_ref": f"L{i}", "category": ln.get("category", "other"), "mapped_group": g, "description": ln.get("description", ""),
                      "qty": ln.get("qty", "1"), "unit_price": ln.get("unit_price", ln["amount"]), "claimed_amount": ln["amount"],
                      "is_non_medical": bool(m and m.is_non_medical), "is_implant": bool(m and m.is_implant), "exclusion_tags": list(m.tags) if m else [],
                      "procedure_group": group if group and g in PROCEDURE_LINE_GROUPS else None})  # fmt: skip
    room = terms.get("room_rent_percent", {}).get("value")
    rules = PolicyRules.model_validate({
        "room_rent": {"percent": room or "100", "icu_percent": terms.get("icu_percent", {}).get("value") or "100"},
        "co_pay": {"percent": terms.get("co_pay_percent", {}).get("value") or "0"},
        "sub_limits": {group: terms["sub_limit"]["value"]} if group and "sub_limit" in terms else {},
        # waiting periods and exclusions need the insurer's member history: the insurer checks them
        "waiting_periods_days": {"initial": 0, "pre_existing": 0},
        "exclusions": {"icd_prefixes": [], "tags": ["cosmetic", "non_medical"], "non_medical_policy": "exclude_all"},
    })  # fmt: skip
    return CalcInput.model_validate({
        "case_id": str(uuid.uuid4()), "claim_type": claim_type, "admission_type": adm["admission_type"],
        "policy": {"policy_number": pat.get("policy_number") or "unknown", "product_code": pol["product_code"], "status": "active",
                   "start_date": start, "end_date": end, "premium_paid_until": end, "sum_insured": str(pol["sum_insured"])},
        "member": {"member_id": pat.get("member_id") or "unknown", "dob": pat["dob"], "cover_start": start},
        "admission": {"admitted_on": admitted, "discharged_on": discharged, "admission_type": adm["admission_type"],
                      "diagnosis_codes": adm.get("diagnosis_codes") or [], "procedure_codes": adm.get("procedure_codes") or [],
                      "procedure_group": group, "hospital": {"hospital_id": "self", "network_status": "network"}},
        "lines": lines, "rules": rules, "rules_version": 1,
    })  # fmt: skip


def _unavailable(reason: str, **kw: Any) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, **kw}


async def estimate(
    ctx: dict[str, Any], payload: dict[str, Any], llm: LLM, st: Settings, rag: Rag | None
) -> dict[str, Any]:
    """Never raises: the claim build must not depend on the estimate."""
    pol = policy_from_card(ctx)
    if not pol["product_code"] or not pol["sum_insured"]:
        return _unavailable("policy card with product and sum insured not found")
    if rag is None:
        return _unavailable(
            "knowledge base not configured (HOSP_RAG_URL)", product_code=pol["product_code"]
        )
    proc = procedure_of(payload)
    filters = {"policy_product": pol["product_code"], "as_of": payload["admission"]["admitted_on"]}
    query = f"{pol['product_code']} room rent limit ICU limit co-payment procedure sub-limits {proc[0] if proc else ''}".strip()
    try:
        chunks = await rag.search(COLLECTION, query, filters)
    except RagUnavailable as e:
        return _unavailable(f"knowledge base not reachable ({e})", product_code=pol["product_code"])
    if not chunks:
        return _unavailable(
            "no policy wording found for this product", product_code=pol["product_code"]
        )
    terms, info, warnings = await read_terms(
        chunks, pol["product_code"], pol["sum_insured"], proc[0] if proc else None, llm, st
    )
    if "room_rent_percent" not in terms:
        return _unavailable(
            "room rent limit not found in the policy wording",
            product_code=pol["product_code"],
            warnings=warnings,
        )
    try:
        from calc_engine.engine import run

        res = run(
            calc_input(
                payload, pol, terms, proc[1] if proc else None, ctx.get("claim_type") or "cashless"
            )
        )
    except Exception as e:  # noqa: BLE001 - a calculation problem is reported, not raised
        return _unavailable(
            f"calculation failed: {type(e).__name__}",
            product_code=pol["product_code"],
            warnings=warnings,
        )
    return {
        "status": "estimated",
        "product_code": pol["product_code"],
        "sum_insured": str(pol["sum_insured"]),
        "claimed_total": str(res.claimed_total),
        "eligible_total": str(res.eligible_total),
        "estimated_payable": str(res.payable_total),
        "patient_pays": str(res.patient_pays_total),
        "deductions": [d.model_dump(mode="json") for d in res.summary_deductions],
        "flags": [f.code.value for f in res.flags],
        "terms": terms,
        "procedure": proc[0] if proc else None,
        "assumptions": [
            "policy in force and premium paid",
            "network hospital",
            "waiting periods and exclusions are checked by the insurer",
        ],
        "engine_version": res.engine_version,
        "model_info": info,
        "warnings": warnings,
    }
