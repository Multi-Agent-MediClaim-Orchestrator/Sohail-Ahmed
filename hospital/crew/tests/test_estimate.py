"""Policy Estimate crew: policy card + hospital-side policy wording + CrewAI agent (quotes checked) + calc engine."""

from decimal import Decimal

import pytest
from crew import estimate
from crew.agents import builder
from crew.flows import ClaimFlow, FlowDeps
from crew.llm import FakeLLM, RulesLLM
from crew.settings import Settings
from rag_service.corpus import PRODUCTS, wording
from test_crew import FakeApi, ctx

ST = Settings()
CARD = {"id": "44444444-4444-4444-8444-444444444444", "doc_type": "policy_card", "pages": 1,
        "typed": {"product_name": "HEALTH-BASIC", "sum_insured": "Rs. 5,00,000.00", "valid_from": "01/01/2026", "valid_to": "31/12/2026"}}  # fmt: skip


def chunks(product="HEALTH-BASIC", v=3):
    text = wording(product, next(x for x in PRODUCTS[product]["versions"] if x["v"] == v))
    return [
        {"citation_id": f"hr-pw#{i}", "text": "### " + part}
        for i, part in enumerate(text.split("\n### ")[1:], 1)
    ]


class FakeRag:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def search(self, collection, query, filters, top_k=6):
        self.calls.append((collection, filters))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def case_with_card(**over):
    c = ctx(documents=[*ctx()["documents"], CARD], **over)
    return c, (await builder.build(c, RulesLLM(), ST))["payload"]


async def test_estimate_reads_terms_and_runs_the_engine():
    c, payload = await case_with_card()
    rag = FakeRag(chunks())
    out = await estimate.estimate(c, payload, RulesLLM(), ST, rag)
    assert out["status"] == "estimated", out
    assert rag.calls[0] == (
        "hosp_insurer_rules",
        {"policy_product": "HEALTH-BASIC", "as_of": "2026-09-28"},
    )
    t = out["terms"]
    assert (
        t["room_rent_percent"]["value"] == "1.0"
        and t["co_pay_percent"]["value"] == "10"
        and t["icu_percent"]["value"] == "2.0"
    )
    assert all(x["read_by"] == "code" and x["citation"].startswith("hr-pw#") for x in t.values())
    assert out["claimed_total"] == "10800.00" and out["sum_insured"] == "500000.00"
    eligible, payable = Decimal(out["eligible_total"]), Decimal(out["estimated_payable"])
    assert payable == (eligible * Decimal("0.9")).quantize(
        Decimal("0.01")
    )  # 10% co-pay from the wording
    assert Decimal(out["patient_pays"]) == Decimal(out["claimed_total"]) - payable


async def test_sub_limit_for_the_procedure_comes_from_the_wording_table():
    c, payload = await case_with_card()
    payload["admission"]["diagnosis_codes"] = ["K80.20"]
    out = await estimate.estimate(c, payload, RulesLLM(), ST, FakeRag(chunks()))
    assert out["procedure"] == "Cholecystectomy"
    # HEALTH-BASIC v3 row: base 20000 + 8000*3 + 1000*3 = 47,000 at 3 lakh; x1.5 at 5 lakh
    assert (
        out["terms"]["sub_limit"]["value"] == "70500"
        and "| Cholecystectomy |" in out["terms"]["sub_limit"]["quote"]
    )


async def test_agent_number_is_overridden_by_code_and_bad_quotes_are_retried():
    c, payload = await case_with_card()
    wrong = {"room_rent_percent": "2.0", "co_pay_percent": "10", "icu_percent": None, "sub_limit": None,
             "citations": [{"term": "room_rent_percent", "source_id": "W1", "quote": "ICU charges are limited to 2.0% of the sum insured"},
                           {"term": "co_pay_percent", "source_id": "W1", "quote": "invented text 10"}]}  # fmt: skip
    src = chunks()
    w = next(i for i, x in enumerate(src, 1) if "ICU charges" in x["text"])
    wrong["citations"][0]["source_id"] = f"W{w}"
    llm = FakeLLM({"<wording>": wrong})
    out = await estimate.estimate(c, payload, llm, ST, FakeRag(src))
    assert (
        len(llm.calls) == 2 and "Fix these terms" in llm.calls[1][1]
    )  # guardrail retry with the problems
    assert (
        out["terms"]["room_rent_percent"]["value"] == "1.0"
        and "term_overridden_by_code:room_rent_percent" in out["warnings"]
    )
    assert (
        "agent_term_dropped:co_pay_percent" in out["warnings"]
        and out["terms"]["co_pay_percent"]["read_by"] == "code"
    )


async def test_agent_term_is_used_when_the_code_cannot_read_the_wording():
    c, payload = await case_with_card()
    src = [{"citation_id": "x#1", "text": "Room charges are payable up to 1.5 percent of the sum insured for each day."},
           {"citation_id": "x#2", "text": "The insured person shares 20 percent of each admissible claim."}]  # fmt: skip
    llm = FakeLLM({"<wording>": {"room_rent_percent": "1.5", "icu_percent": None, "co_pay_percent": "20", "sub_limit": None,
                                 "citations": [{"term": "room_rent_percent", "source_id": "W1", "quote": "up to 1.5 percent of the sum insured"},
                                               {"term": "co_pay_percent", "source_id": "W2", "quote": "shares 20 percent"}]}})  # fmt: skip
    out = await estimate.estimate(c, payload, llm, ST, FakeRag(src))
    assert out["status"] == "estimated" and out["terms"]["room_rent_percent"]["read_by"] == "agent"
    assert Decimal(out["estimated_payable"]) == (
        Decimal(out["eligible_total"]) * Decimal("0.8")
    ).quantize(Decimal("0.01"))


@pytest.mark.parametrize(
    "docs,rag,reason",
    [
        (None, FakeRag([]), "policy card"),
        ([CARD], None, "not configured"),
        ([CARD], FakeRag(estimate.RagUnavailable("ConnectError")), "not reachable"),
        ([CARD], FakeRag([]), "no policy wording"),
        (
            [CARD],
            FakeRag([{"citation_id": "x", "text": "Premiums are payable annually."}]),
            "room rent limit not found",
        ),
    ],
)
async def test_estimate_is_unavailable_never_an_error(docs, rag, reason):
    c = ctx(documents=[*ctx()["documents"], *(docs or [])])
    payload = (await builder.build(c, RulesLLM(), ST))["payload"]
    out = await estimate.estimate(c, payload, RulesLLM(), ST, rag)
    assert out["status"] == "unavailable" and reason in out["reason"]


async def test_claim_flow_posts_the_estimate_with_the_draft():
    c, _ = await case_with_card()
    api = FakeApi({"/v1/internal/cases/c1/build-context": c})
    flow = ClaimFlow.create(FlowDeps(RulesLLM(), api, ST, rag=FakeRag(chunks())))
    out = await flow.kickoff_async(inputs={"case_id": "c1", "job_id": "j1"})
    assert out["estimate"] == "estimated"
    body = api.posts[0][1]
    assert (
        body["estimate"]["status"] == "estimated"
        and body["estimate"]["product_code"] == "HEALTH-BASIC"
    )


async def test_estimate_against_the_real_rag_service_with_the_hospital_crew_token():
    """Real rag-service app in process (memory store, hash embedder): ingestion, as_of filter and the access rules apply."""
    import httpx
    from crew.estimate import HttpRag
    from crew.settings import rag_token_from_secret
    from rag_service import corpus
    from rag_service import ingest as ing
    from rag_service.app import create_app
    from rag_service.config import Settings as RagSettings
    from rag_service.db import Meta
    from rag_service.store import MemoryStore
    from rag_service.text import HashEmbedder

    cfg = RagSettings(jwt_secret="test-secret-0123456789012345678901234567")
    store, meta, emb = MemoryStore(), Meta(":memory:"), HashEmbedder(cfg.embed_dim)
    for d in corpus.build_kb():
        ing.ingest_markdown(store, meta, emb, d.collection, d.markdown, d.meta, supersede=True)
    app = create_app(cfg, store=store, meta=meta, embedder=emb)
    rag = HttpRag(
        "http://rag", rag_token_from_secret(cfg.jwt_secret), transport=httpx.ASGITransport(app=app)
    )
    c, payload = await case_with_card()
    out = await estimate.estimate(c, payload, RulesLLM(), ST, rag)
    assert out["status"] == "estimated", out
    assert (
        out["terms"]["room_rent_percent"]["value"] == "1.0"
        and out["terms"]["co_pay_percent"]["value"] == "10"
    )  # v3, in force on 2026-09-28
    assert all(t["citation"].startswith("hr-pw-HPA-v3") for t in out["terms"].values())
    with pytest.raises(
        estimate.RagUnavailable
    ):  # the hospital token cannot read the insurer's collections
        await rag.search("ins_policy_wording", "room rent", {"policy_product": "HEALTH-BASIC"})
