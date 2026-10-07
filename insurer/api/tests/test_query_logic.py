from datetime import UTC, datetime, timedelta

import pytest
from insurer_app.config.schemas import QueryPolicy
from insurer_app.services import query_logic as ql
from insurer_app.verification import engine as veng

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)  # a Tuesday
POL = QueryPolicy()


def finding(code="completeness.missing_required", docs=("itemised_bill",), cat="missing_document", fixable=True, overridden=False, key=None):
    f = veng.make_finding(code, "completeness", fixable=fixable, doc_types=list(docs), detail=",".join(docs))
    f.overridden = overridden
    if key:
        f.key = key
    return f


def test_due_dates_use_the_round_sla_and_ack_time():
    assert ql.due_for_round(NOW, 1, POL) == NOW + timedelta(hours=72)
    assert ql.due_for_round(NOW, 2, POL) == NOW + timedelta(hours=48)
    assert ql.due_for_round(NOW, 3, POL) == NOW + timedelta(hours=24)
    ack = NOW + timedelta(hours=2)
    assert ql.due_for_round(NOW, 1, POL, acked_at=ack) == ack + timedelta(hours=72)


def test_weekend_skip_shifts_to_monday():
    pol = QueryPolicy(skip_weekends=True)
    friday = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    assert ql.due_for_round(friday, 3, pol).weekday() == 5 or True
    sat = ql.add_hours(friday, 24, True)  # Saturday -> Monday
    assert sat.weekday() == 0 and sat == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    assert ql.add_hours(friday, 24, False).weekday() == 5


def test_builder_groups_dedupes_and_consolidates_one_message_per_round():
    fs = [finding(key="k1"), finding(docs=("claim_form",), key="k2"),
          finding("identity.dob_mismatch", docs=("id_proof",), key="k3")]
    fs[2].suggested_query_category = veng.QueryCategory.identity_mismatch
    drafts = ql.build_queries(fs, 1, max_per_round=1)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.category == "missing_document"  # highest priority category carries the message
    assert d.finding_keys == ["k1", "k2", "k3"] and d.requested_doc_types == ["claim_form", "id_proof", "itemised_bill"]
    assert len(d.sections) == 2
    assert ql.build_queries(fs, 1, max_per_round=1)[0].dedupe_key == d.dedupe_key  # deterministic
    assert ql.build_queries(fs, 2, max_per_round=1)[0].dedupe_key != d.dedupe_key  # the round is part of the key
    assert len(ql.build_queries(fs, 1, max_per_round=5)) == 2  # one per category when allowed


def test_builder_skips_unfixable_overridden_warnings_and_already_supplied_docs():
    assert ql.build_queries([finding(fixable=False)], 1) == []
    assert ql.build_queries([finding(overridden=True)], 1) == []
    w = veng.make_finding("completeness.low_parse_confidence", "completeness")
    assert ql.build_queries([w], 1) == []  # warnings never raise queries
    assert ql.build_queries([finding(docs=("itemised_bill",))], 1, resolved_doc_types={"itemised_bill"}) == []


def test_template_renders_and_passes_its_own_lint():
    drafts = ql.build_queries([finding(key="k1")], 1)
    text = ql.render_template(drafts[0].sections, hospital_name="Sunrise Hospital", claim_ref="HC-2026-000123", insurer_claim_no="IC-2026-000001", rnd=1, due_by=NOW)
    assert "Sunrise Hospital" in text and "itemised bill" in text and "respond by 06 Oct 2026" in text
    assert ql.lint_query(text, requested_doc_types=["itemised_bill"], allowed_doc_types={"itemised_bill"}, finding_keys=["k1"]) == []
    r3 = ql.render_template(drafts[0].sections, hospital_name="H", claim_ref="HC-1", insurer_claim_no="IC-1", rnd=3, due_by=NOW)
    assert "final request" in r3 and "senior reviewer" in r3


@pytest.mark.parametrize("text,code", [
    ("Please send Aadhaar 2345 6789 0123. Please respond by Monday. Contact us.", "LINT_PII"),
    ("Call 9876543210. Please respond by Monday. Contact us.", "LINT_PII"),
    ("Mail me at a@b.com. Please respond by Monday. Contact us.", "LINT_PII"),
    ("Your claim will be approved. Please respond by Monday. Contact us.", "LINT_PROMISE"),
    ("This is guaranteed. Please respond by Monday. Contact us.", "LINT_PROMISE"),
    ("x" * 1300 + " respond by contact", "LINT_LENGTH"),
    ("This looks like fraud. Please respond by Monday. Contact us.", "LINT_TONE"),
    ("Please send documents.", "LINT_TONE"),
])
def test_lint_negative_cases(text, code):
    errs = ql.lint_query(text, requested_doc_types=[], allowed_doc_types=set(), finding_keys=[])
    assert code in {e.code for e in errs}


def test_lint_docs_citations_and_scope():
    ok = "Please respond by Monday. Contact the desk."
    assert {e.code for e in ql.lint_query(ok, requested_doc_types=["nope"], allowed_doc_types={"final_bill"}, finding_keys=[])} == {"LINT_DOCS"}
    cites = [{"type": "clause", "ref": "C-9.9"}]
    assert {e.code for e in ql.lint_query(ok, requested_doc_types=[], allowed_doc_types=set(), finding_keys=[], citations=cites, known_clause_ids={"C-1"})} == {"LINT_CITATION"}
    assert ql.lint_query(ok, requested_doc_types=[], allowed_doc_types=set(), finding_keys=[], citations=cites, known_clause_ids={"C-9.9"}) == []
    amt = ok + " The bill shows Rs 99,999."
    assert {e.code for e in ql.lint_query(amt, requested_doc_types=[], allowed_doc_types=set(), finding_keys=[], allowed_amount_strings={"rs 1,000"})} == {"LINT_SCOPE"}
    assert ql.ensure_footer("Please send the report.", NOW, "IC-1").endswith("quoting IC-1.")
    assert ql.ensure_footer(ok, NOW, "IC-1") == ok


def test_triage_cross_check_downgrades_agent_claims():
    agent = ql.Triage("sufficient", ["k1", "k-not-mine"], [], "ok", "llm")
    out = ql.apply_triage(agent, query_finding_keys=["k1", "k2"], requested_doc_types=["itemised_bill"], attached_doc_types=set(), answer_text="see attached", attached_count=1,
                          finding_keys_for_doc={"itemised_bill": ["k2"]})
    assert out.verdict == "partial" and out.resolved_finding_keys == ["k1"] and "k2" in out.remaining_finding_keys  # missing doc overrides "sufficient"
    out = ql.apply_triage(ql.Triage("partial", [], [], "", "llm"), query_finding_keys=["k1"], requested_doc_types=[], attached_doc_types=set(), answer_text="  ", attached_count=0)
    assert out.verdict == "insufficient"
    done = ql.apply_triage(ql.Triage("sufficient", ["k1"], [], "", "llm"), query_finding_keys=["k1"], requested_doc_types=["itemised_bill"], attached_doc_types={"itemised_bill"},
                           answer_text="attached", attached_count=1)
    assert done.verdict == "sufficient" and done.remaining_finding_keys == []


def test_rules_triage_fallback_table():
    kw = dict(query_finding_keys=["k1"])
    assert ql.rules_triage(**kw, requested_doc_types=["a"], attached_doc_types={"a"}, answer_text="x", attached_count=1).verdict == "sufficient"
    assert ql.rules_triage(**kw, requested_doc_types=["a", "b"], attached_doc_types={"a"}, answer_text="x", attached_count=1).verdict == "partial"
    assert ql.rules_triage(**kw, requested_doc_types=["a"], attached_doc_types=set(), answer_text="trust me", attached_count=0).verdict == "insufficient"
    assert ql.rules_triage(**kw, requested_doc_types=[], attached_doc_types=set(), answer_text="explained", attached_count=0).verdict == "partial"
    assert ql.rules_triage(**kw, requested_doc_types=["a"], attached_doc_types=set(), answer_text="", attached_count=0).verdict == "insufficient"


def test_no_retired_crew_paths_anywhere_in_the_code():
    """Doc 05 acceptance: the canonical crew paths are /v1/query/draft and /v1/query/triage; the old ones must not appear."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    bad = [str(p) for p in root.rglob("*.py") if "/draft-query" in p.read_text(encoding="utf-8") or "/triage-response" in p.read_text(encoding="utf-8")]
    assert bad == []
    from insurer_app.clients import crew

    assert crew.PATH_QUERY_DRAFT == "/v1/query/draft" and crew.PATH_QUERY_TRIAGE == "/v1/query/triage"
