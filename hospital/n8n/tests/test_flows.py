"""Doc 08 §9 scenarios against a real n8n with stubbed services."""

import time
import uuid

import pytest
from n8n_helpers import fire, wait_for

pytestmark = pytest.mark.n8n
DOC = {"case_id": "c1", "document_id": "d1"}


def uploaded(n8n, stub, **kw):  # type: ignore[no-untyped-def]
    stub.reset()
    return fire(n8n, "intake/document-uploaded", DOC, idem=kw.get("idem", str(uuid.uuid4())))


def test_t01_clean_pdf_golden_order(n8n, stub):  # type: ignore[no-untyped-def]
    code, body = uploaded(n8n, stub)
    assert code == 200 and body == {"accepted": True}
    assert wait_for(lambda: any("/classify" in c for c in stub.paths()))
    seq = [c for c in stub.paths() if "jobs/pj-1" not in c]
    assert seq == [
        "POST /v1/internal/idempotency",
        "GET /v1/internal/documents/d1",
        "POST /vision/v1/quality",
        "POST /v1/internal/documents/d1/quality",
        "POST /docpipe/v1/parse",
        "POST /v1/internal/documents/d1/parse",
        "POST /v1/internal/documents/d1/parse",
        "POST /v1/internal/documents/d1/classify",
    ]
    assert stub.body("POST", "/v1/internal/documents/d1/classify")["doc_type"] == "final_bill"


def test_t02_blurry_blocks_parse(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.quality = {"quality_score": 0.1, "flags": ["blurry"], "has_required_stamp": None}
    fire(n8n, "intake/document-uploaded", DOC, idem=str(uuid.uuid4()))
    assert wait_for(lambda: any("/status" in c for c in stub.paths()))
    assert stub.body("POST", "/v1/internal/documents/d1/status")["parse_status"] == "needs_review"
    assert not any("/parse" in c for c in stub.paths())


def test_t03_not_clean_stops(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.scan_status = "infected"
    fire(n8n, "intake/document-uploaded", DOC, idem=str(uuid.uuid4()))
    time.sleep(4)
    assert stub.paths() == ["POST /v1/internal/idempotency", "GET /v1/internal/documents/d1"]


def test_t05_pipeline_timeout_marks_failed(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.parse_state = "running"
    fire(n8n, "intake/document-uploaded", DOC, idem=str(uuid.uuid4()))
    assert wait_for(lambda: stub.body("POST", "/v1/internal/documents/d1/status") is not None, 40)
    assert stub.body("POST", "/v1/internal/documents/d1/status")["parse_status"] == "failed"


def test_t06_pipeline_failure_marks_failed(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.parse_state = "failed"
    fire(n8n, "intake/document-uploaded", DOC, idem=str(uuid.uuid4()))
    assert wait_for(lambda: stub.body("POST", "/v1/internal/documents/d1/status") is not None)
    assert "boom" in stub.body("POST", "/v1/internal/documents/d1/status")["error"]


def test_t08_duplicate_webhook(n8n, stub):  # type: ignore[no-untyped-def]
    key = str(uuid.uuid4())
    uploaded(n8n, stub, idem=key)
    assert wait_for(lambda: any("/classify" in c for c in stub.paths()))
    before = len(stub.calls)
    fire(n8n, "intake/document-uploaded", DOC, idem=key)
    time.sleep(3)
    assert [c for c in stub.paths()][
        len(stub.paths()) - 1
    ] != "GET /v1/internal/documents/d1" or len(stub.calls) == before + 1
    assert sum(1 for c in stub.paths() if c == "POST /docpipe/v1/parse") == 1


def test_t09_wrong_secret(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    code, body = fire(n8n, "intake/document-uploaded", DOC, secret="nope")
    assert code == 401
    time.sleep(2)
    assert stub.paths() == []


def test_t16_crew_down_keeps_rules_triage(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.triage_state = "down"
    fire(n8n, "query/intake", {"query_id": "q1"}, idem=str(uuid.uuid4()))
    assert wait_for(lambda: any("/notify" in c for c in stub.paths()))
    assert stub.body("POST", "/v1/internal/cases/c1/notify")["event"] == "query.needs_owner"
    assert not any("triage-result" in c for c in stub.paths())


def test_query_intake_auto_draft_and_round3(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    fire(n8n, "query/intake", {"query_id": "q1"}, idem=str(uuid.uuid4()))
    assert wait_for(lambda: any("query-draft" in c for c in stub.paths()))
    assert any("triage-result" in c for c in stub.paths())
    stub.reset()
    stub.query["round"] = 3  # t17: round 3 is never drafted automatically
    fire(n8n, "query/intake", {"query_id": "q1"}, idem=str(uuid.uuid4()))
    assert wait_for(lambda: any("/notify" in c for c in stub.paths()))
    assert not any("query-draft" in c for c in stub.paths())


def test_t18_reminders_batches(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.reminders = [
        {"id": f"r{i}", "case_id": "c1", "kind": "doc_request", "channel": "in_app"}
        for i in range(12)
    ]
    stub.reminders.append({"id": "rx", "case_id": "c1", "kind": "doc_request", "channel": "email"})
    fire(n8n, "jobs/reminders", {})
    assert wait_for(lambda: len([c for c in stub.paths() if c.endswith("/fired")]) == 12)
    fired = [c for c in stub.paths() if c.endswith("/fired")]
    assert len(fired) == 12 and any(c.endswith("/rx/failed") for c in stub.paths())


def test_f7_sweeper_and_stalled_outbox(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.outbox_stalled = [{"case_id": "c1", "kind": "claim.submit", "status": "dead"}]
    fire(n8n, "jobs/sweeper", {})
    assert wait_for(lambda: stub.body("POST", "/v1/internal/ops/events") is not None)
    assert "POST /v1/internal/jobs/sweeper" in stub.paths()
    assert stub.body("POST", "/v1/internal/ops/events")["severity"] == "high"


def test_t20_node_error_reaches_global_handler(n8n, stub):  # type: ignore[no-untyped-def]
    stub.reset()
    stub.doc_get_status = 500  # node 08 has no error branch: the execution fails and F8 reports it
    fire(n8n, "intake/document-uploaded", DOC, idem=str(uuid.uuid4()))
    assert wait_for(lambda: stub.body("POST", "/v1/internal/ops/events") is not None, 40)
    assert stub.body("POST", "/v1/internal/ops/events")["workflow"] == "hosp-intake-document"


def test_t15_unacknowledged_claim_banner(n8n, stub):  # type: ignore[no-untyped-def]
    """The 15-minute wait is exercised by the structure test in test_lint (time cannot be mocked in n8n)."""
    stub.reset()
    code, _ = fire(n8n, "claim/submitted", {"case_id": "c1"}, idem=str(uuid.uuid4()))
    assert code == 200 and wait_for(lambda: "POST /v1/internal/idempotency" in stub.paths())
