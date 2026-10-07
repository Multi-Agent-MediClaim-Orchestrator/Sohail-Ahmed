"""The same retrieval/eval behaviour against a REAL Qdrant (docker): hybrid dense+sparse, temporal filter, aliases, reindex, delete-by-filter."""

from __future__ import annotations

import pytest
from rag_service import corpus
from rag_service import evaluation as ev
from rag_service import ingest as ing
from rag_service.db import Meta
from rag_service.qdrant_store import QdrantError, QdrantStore
from rag_service.retrieval import LexicalReranker, SearchParams, search
from rag_service.store import Filter
from rag_service.text import HashEmbedder

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live(qdrant_url):
    q = QdrantStore(qdrant_url)
    assert q.wait_ready(30)
    meta, emb = Meta(":memory:"), HashEmbedder()
    for d in corpus.build_kb():
        ing.ingest_markdown(q, meta, emb, d.collection, d.markdown, d.meta)
    return q, meta, emb


def test_collections_are_aliases_with_payload_indexes_and_counts(live):
    q, _, _ = live
    stats = q.stats()
    assert {"ins_policy_wording", "ins_medical_guidelines", "hosp_insurer_rules", "ins_case_history"} <= set(stats)
    assert stats["ins_policy_wording"]["physical"] == "ins_policy_wording__v1" and stats["ins_policy_wording"]["points"] > 100
    info = q._call("GET", "/collections/ins_policy_wording__v1")["result"]
    assert {"system", "policy_product", "effective_from", "case_id"} <= set(info["payload_schema"])
    assert info["config"]["params"]["vectors"]["dense"]["size"] == 768


def test_reingest_is_idempotent_on_live_qdrant(live):
    q, meta, emb = live
    d = corpus.build_kb()[0]
    before = q.count(d.collection)
    rep = ing.ingest_markdown(q, meta, emb, d.collection, d.markdown, d.meta)
    assert rep.chunks_added == 0 and q.count(d.collection) == before


def test_temporal_filter_and_code_lookup_match_memory_store_behaviour(live):
    q, _, emb = live
    for as_of, room in (("2025-09-01", "1.5"), ("2026-03-01", "1.25"), ("2026-09-01", "1.0")):
        res = search(q, emb, "ins_policy_wording", "room rent limit per day", {"policy_product": "HealthPlus-A", "as_of": as_of}, SearchParams(rerank=False))
        assert res.results and any(f"limited to {room}%" in r["text"] for r in res.results[:3]), (as_of, [r["text"][:60] for r in res.results])
    g = search(q, emb, "ins_medical_guidelines", "expected length of stay for I21.9", {}, SearchParams(rerank=False))
    assert any("I21.9" in r["text"] for r in g.results[:3])
    miss = search(q, emb, "ins_policy_wording", "room rent", {"policy_product": "FamilyShield", "as_of": "2025-01-01"}, SearchParams())
    assert miss.results == [] and miss.diagnostics["temporal_miss"] is True


def test_eval_targets_hold_on_live_qdrant(live):
    q, _, emb = live
    rows = ev.resolve_gold(q, corpus.build_qa())
    m = ev.evaluate(q, emb, rows)
    assert m["recall@5"] >= 0.85 and m["mrr"] >= 0.6 and m["temporal_trap_accuracy"] >= 0.95 and m["insufficient_evidence_accuracy"] >= 0.9


def test_delete_where_and_reindex_swap_alias_on_live_qdrant(live):
    q, meta, _ = live
    ing.ingest_case_text(q, meta, HashEmbedder(), "c1f00000-0000-0000-0000-000000000001", "doc-a", "discharge_summary", [{"page": 1, "text": "Patient <PERSON_1> was admitted with chest pain."}])
    assert q.count("hosp_case_context", Filter(match={"case_id": ["c1f00000-0000-0000-0000-000000000001"]})) == 1
    assert ing.purge_case(q, "c1f00000-0000-0000-0000-000000000001") == 1 and q.count("hosp_case_context") == 0
    out = ing.reindex(q, meta, HashEmbedder(), "ins_medical_guidelines", ts="live1")
    assert out["points"] > 0 and out["physical"].endswith("__reindex_live1") and out["dropped"] == "ins_medical_guidelines__v1"
    assert q.stats()["ins_medical_guidelines"]["physical"] == out["physical"] and q.count("ins_medical_guidelines") == out["points"]
    assert search(q, HashEmbedder(), "ins_medical_guidelines", "expected length of stay I21.9", {}, SearchParams(rerank=False)).results
    with pytest.raises(QdrantError):  # unfiltered delete is refused client-side
        q.delete_where("ins_medical_guidelines", Filter())
_ = LexicalReranker
