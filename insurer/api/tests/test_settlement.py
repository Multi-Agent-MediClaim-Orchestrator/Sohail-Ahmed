import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from claim_contract.insurer_side.samples import make_line, make_submission, money
from claim_contract.models import SettlementNotice
from ins_helpers import register_claim_docs, unique_member
from insurer_app.clients import bank_sim
from insurer_app.services import audit, jobs, outbox, settlement
from insurer_app.services.settlement import net_amount, retry_delay, valid_utr
from sqlalchemy import text
from tpa_sim.bank import BankSim
from tpa_sim.bank_api import build_bank_app, make_callback_sender

pytestmark = pytest.mark.integration
BANK_SECRET = b"dev-bank-sim-secret-000000000000000000"
CB_SECRET = b"dev-bank-sim-callback-secret-00000000000"


def approved_claim(amount="12000.00", claim_type="cashless", **kw):
    ref = f"HC-2026-{uuid.uuid4().int % 900000 + 100000}"
    c = make_submission(claim_ref=ref, claim_type=claim_type, doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **{**unique_member(), **kw})
    c["bill_lines"] = [make_line(1, category="surgery", desc="Surgeon fee", qty="1", unit=amount, doc_id=c["documents"][1]["doc_id"])]
    c["totals"] = {"gross": money(amount), "discounts": money("0"), "claimed": money(amount)}
    return c


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


@pytest.fixture
async def bank(env):
    """Bank simulator wired to the insurer app in-process (no network), with a programmable profile."""
    b = BankSim()
    b.callback = make_callback_sender("http://insurer", CB_SECRET, transport=httpx.ASGITransport(app=env.app))
    app = build_bank_app(b, BANK_SECRET)
    bank_sim.set_transport(httpx.ASGITransport(app=app))
    yield b
    bank_sim.set_transport(None)


async def approved_case(env, **kw):
    claim = approved_claim(**kw)
    register_claim_docs(env, claim)
    await env.sim.submit(claim)
    await jobs.drain({"fetch_documents", "start_verification"})
    row = (await q(env, "SELECT id, status::text AS st FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0]
    assert row.st == "approved", row.st  # clean + small => auto-approved
    return row.id, claim


async def run_settlement(env, bank, case_id, profile="always_pay"):
    bank.default_profile = profile
    jobs.clear()
    await settlement.initiate(case_id)
    await bank.settle_all()


async def st(env, case_id):
    return (await q(env, "SELECT status, utr, amount, attempt_count, failure_reason, payee_type FROM core.settlement WHERE case_id = :c ORDER BY created_at DESC", c=case_id))[0]


async def case_status(env, cid):
    return (await q(env, "SELECT status::text AS s FROM core.claim_case WHERE id = :c", c=cid))[0].s


# ------------------------------------------------------------------ pure
def test_net_amount_examples():
    adv = lambda a: [{"amount": a, "subtractive": True}]  # noqa: E731
    assert net_amount(Decimal("84250"), adv("20000")) == (Decimal("64250.00"), Decimal("0.00"))
    assert net_amount(Decimal("84250"), adv("90000")) == (Decimal("0.00"), Decimal("5750.00"))
    assert net_amount(Decimal("100"), []) == (Decimal("100.00"), Decimal("0.00"))
    assert net_amount(Decimal("100"), [{"amount": "999", "subtractive": False}]) == (Decimal("100.00"), Decimal("0.00"))
    assert [retry_delay(a) for a in (1, 2)] == [timedelta(minutes=5), timedelta(minutes=10)]
    assert valid_utr("SIMUTR20261006000045") and not valid_utr("HDFC123") and not valid_utr("SIMUTR")


def test_live_mode_refuses_to_start():
    from insurer_app.settings import Settings

    with pytest.raises(ValueError):
        Settings(settlement_mode="live")
    with pytest.raises(RuntimeError):
        settlement.assert_sim_mode(type("S", (), {"settlement_mode": "live"})())


# ------------------------------------------------------------------ S1 always pay
async def test_s1_always_pay_settles_notifies_hospital_once_then_closes(env, bank):
    cid, claim = await approved_case(env)
    await run_settlement(env, bank, cid)
    s = await st(env, cid)
    assert s.status == "paid" and s.utr.startswith("SIMUTR") and s.payee_type == "hospital" and str(s.amount) == "12000.00"
    assert await case_status(env, cid) == "settled"
    sender = outbox.make_sender(env.sm, env.sim.receiver_client(), env.settings, backoff_scale=0.0)
    for _ in range(5):
        await sender.run_once()
    notes = [r for r in env.sim.of_kind("settlements") if r.body["claim_ref"] == claim["claim_ref"]]
    assert len(notes) == 1
    parsed = SettlementNotice.from_json_dict(notes[0].body["settlement"])
    assert parsed.status == "paid" and parsed.amount.amount == Decimal("12000.00") and parsed.mode.value == "NEFT"
    env.sim.assert_monotonic_sequences()
    assert await settlement.autoclose() >= 1
    assert await case_status(env, cid) == "closed"
    async with env.sm() as sess:
        assert (await audit.verify(sess, cid)).ok
    ev = [r.event_type for r in await q(env, "SELECT event_type FROM audit.audit_event WHERE case_id = :c", c=cid)]
    assert {"settlement.initiated", "settlement.paid"} <= set(ev)


async def test_initiate_is_idempotent_and_concurrent_calls_make_one_settlement(env, bank):
    import asyncio

    cid, _ = await approved_case(env)
    bank.default_profile = "slow_5min"
    jobs.clear()
    rs = await asyncio.gather(settlement.initiate(cid), settlement.initiate(cid), settlement.initiate(cid))
    assert sum(1 for r in rs if not r.get("existing")) == 1
    assert (await q(env, "SELECT count(*) FROM core.settlement WHERE case_id = :c", c=cid))[0][0] == 1
    assert len(bank.payouts) == 1  # S8: no duplicate payout request
    assert (await st(env, cid)).status == "initiated"
    assert await bank.release() == 1
    await bank.settle_all()
    assert (await st(env, cid)).status == "paid"


# ------------------------------------------------------------------ S2 / S3 / S4 failures
async def test_s2_fail_once_then_pay_retries_with_a_new_idempotency_key(env, bank):
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid, "fail_once_then_pay")
    s = await st(env, cid)
    assert s.status == "failed" and s.attempt_count == 1 and s.failure_reason == "bank_down"
    nxt = (await q(env, "SELECT next_retry_at FROM core.settlement WHERE case_id = :c", c=cid))[0].next_retry_at
    assert nxt is not None and nxt > datetime.now(UTC)
    assert await settlement.retry_due(datetime.now(UTC)) == 0  # not due yet
    assert await settlement.retry_due(datetime.now(UTC) + timedelta(minutes=6)) == 1
    await bank.settle_all()
    assert (await st(env, cid)).status == "paid" and len(bank.payouts) == 2


async def test_s3_always_fail_bank_down_gives_three_attempts_then_a_task(env, bank):
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid, "always_fail:bank_down")
    for minutes in (6, 16):
        await settlement.retry_due(datetime.now(UTC) + timedelta(minutes=minutes))
        await bank.settle_all()
    s = await st(env, cid)
    assert s.status == "failed" and s.attempt_count == 3
    tasks = await q(env, "SELECT kind, status FROM core.settlement_task WHERE case_id = :c", c=cid)
    assert [(t.kind, t.status) for t in tasks] == [("settlement_failed", "open")]
    assert await settlement.retry_due(datetime.now(UTC) + timedelta(days=1)) == 0
    async with env.client("senior1", ["senior_reviewer"]) as cl:
        sid = (await q(env, "SELECT id FROM core.settlement WHERE case_id = :c", c=cid))[0].id
        assert (await cl.post(f"/v1/settlements/{sid}/retry", json={"note": ""})).status_code == 422
        bank.default_profile = "always_pay"
        assert (await cl.post(f"/v1/settlements/{sid}/retry", json={"note": "bank is back up"})).status_code == 200
        await bank.settle_all()
        assert (await cl.post(f"/v1/settlements/{sid}/retry", json={"note": "again"})).status_code == 409  # only from failed
        assert (await cl.get("/v1/settlement-tasks")).status_code == 200
    assert (await st(env, cid)).status == "paid"
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.post(f"/v1/settlements/{sid}/retry", json={"note": "x"})).status_code == 403


async def test_s4_non_retryable_failure_opens_a_task_immediately(env, bank):
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid, "always_fail:invalid_account")
    s = await st(env, cid)
    assert s.status == "failed" and s.failure_reason == "invalid_account"
    assert (await q(env, "SELECT next_retry_at FROM core.settlement WHERE case_id = :c", c=cid))[0].next_retry_at is None
    assert (await q(env, "SELECT count(*) FROM core.settlement_task WHERE case_id = :c AND kind = 'settlement_failed'", c=cid))[0][0] == 1


# ------------------------------------------------------------------ S5 reversal
async def test_s5_reverse_after_pay_returns_case_to_approved_with_notice_and_task(env, bank):
    cid, claim = await approved_case(env)
    await run_settlement(env, bank, cid, "reverse_after_pay")
    assert (await st(env, cid)).status == "reversed"
    assert await case_status(env, cid) == "approved"  # settled -> approved only through the reversal service
    assert (await q(env, "SELECT count(*) FROM core.settlement_task WHERE case_id = :c AND kind = 'reversal'", c=cid))[0][0] == 1
    sender = outbox.make_sender(env.sm, env.sim.receiver_client(), env.settings, backoff_scale=0.0)
    for _ in range(6):
        await sender.run_once()
    mine = [r.body["settlement"]["status"] for r in env.sim.of_kind("settlements") if r.body["claim_ref"] == claim["claim_ref"]]
    assert mine == ["paid", "reversed"]
    # utilisation is NOT credited back automatically; a senior can release it once, with a reason
    sid = (await q(env, "SELECT id FROM core.settlement WHERE case_id = :c", c=cid))[0].id
    before = (await q(env, "SELECT utilised_amount FROM core.policy_claim_utilisation u JOIN core.claim_case c ON c.policy_id = u.policy_id WHERE c.id = :c", c=cid))[0].utilised_amount
    async with env.client("senior1", ["senior_reviewer"]) as cl:
        assert (await cl.post(f"/v1/settlements/{sid}/release-utilisation", json={"reason": ""})).status_code == 422
        assert (await cl.post(f"/v1/settlements/{sid}/release-utilisation", json={"reason": "payment reversed by bank"})).status_code == 200
        assert (await cl.post(f"/v1/settlements/{sid}/release-utilisation", json={"reason": "again please"})).status_code == 409
    after = (await q(env, "SELECT utilised_amount FROM core.policy_claim_utilisation u JOIN core.claim_case c ON c.policy_id = u.policy_id WHERE c.id = :c", c=cid))[0].utilised_amount
    assert before - after == Decimal("12000.00")
    # a fresh initiate after the reversal is allowed (partial unique index excludes 'reversed')
    bank.default_profile = "always_pay"
    assert (await settlement.initiate(cid))["status"] == "initiated"
    await bank.settle_all()
    assert (await st(env, cid)).status == "paid"


# ------------------------------------------------------------------ S6 / S7 anomalies
async def test_s6_duplicate_callback_one_notice_and_different_utr_is_an_anomaly(env, bank):
    cid, claim = await approved_case(env)
    await run_settlement(env, bank, cid, "duplicate_callback")
    assert (await st(env, cid)).status == "paid"
    assert (await q(env, "SELECT count(*) FROM ops.outbox WHERE case_id = :c AND endpoint LIKE '%settlements'", c=cid))[0][0] == 1
    cid2, _ = await approved_case(env)
    await run_settlement(env, bank, cid2, "duplicate_callback_diff_utr")
    assert (await st(env, cid2)).status == "paid"
    assert (await q(env, "SELECT count(*) FROM core.settlement_task WHERE case_id = :c AND kind = 'anomaly'", c=cid2))[0][0] == 1
    assert (await q(env, "SELECT count(*) FROM ops.outbox WHERE case_id = :c AND endpoint LIKE '%settlements'", c=cid2))[0][0] == 1


async def test_s7_amount_mismatch_is_an_anomaly_and_not_paid(env, bank):
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid, "amount_mismatch")
    s = await st(env, cid)
    assert s.status == "initiated" and await case_status(env, cid) == "approved"
    assert (await q(env, "SELECT count(*) FROM core.settlement_task WHERE case_id = :c AND kind = 'anomaly'", c=cid))[0][0] == 1


# ------------------------------------------------------------------ callback endpoint security / races
async def test_bank_callback_requires_signature_and_unknown_settlement_is_409(env, bank):
    import json

    body = json.dumps({"settlement_id": str(uuid.uuid4()), "status": "paid", "utr": "SIMUTR20261006000001", "amount": "1.00"}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://insurer") as c:
        unsigned = await c.post("/internal/settlement/bank-callback", content=body)
        assert unsigned.status_code == 401
        from claim_contract import signing

        h = signing.build_headers(CB_SECRET, "bank-sim", "POST", "/internal/settlement/bank-callback", body, str(uuid.uuid4()))
        late = await c.post("/internal/settlement/bank-callback", content=body, headers=h)
        assert late.status_code == 409 and late.json()["code"] == "settlement_not_committed"  # the sim retries
        wrong = signing.build_headers(b"not-the-secret-00000000000000000000", "bank-sim", "POST", "/internal/settlement/bank-callback", body, str(uuid.uuid4()))
        assert (await c.post("/internal/settlement/bank-callback", content=body, headers=wrong)).status_code == 401


async def test_sim_down_at_initiate_keeps_settlement_initiated_and_resends_same_key(env, bank):
    cid, _ = await approved_case(env)
    bank_sim.set_transport(httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    jobs.clear()
    await settlement.initiate(cid)
    s = (await q(env, "SELECT status, idempotency_key, next_retry_at FROM core.settlement WHERE case_id = :c", c=cid))[0]
    assert s.status == "initiated" and s.next_retry_at is not None
    key = s.idempotency_key
    bank_sim.set_transport(httpx.ASGITransport(app=build_bank_app(bank, BANK_SECRET)))
    assert await settlement.retry_due(datetime.now(UTC) + timedelta(minutes=10)) == 1
    await bank.settle_all()
    assert (await st(env, cid)).status == "paid" and str(list(bank.payouts)[0]) == str(key)


# ------------------------------------------------------------------ payee, adjustments, zero amounts, reports, reconciliation
async def test_preauth_advance_is_subtracted_and_excess_opens_a_refund_task(env, bank):
    bank.preauth["PA-2026-33121"] = {"approved_amount": "20000.00", "advance_paid": "5000.00"}
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid)
    s = (await q(env, "SELECT amount, gross_amount, adjustments FROM core.settlement WHERE case_id = :c", c=cid))[0]
    assert (s.gross_amount, s.amount) == (Decimal("12000.00"), Decimal("7000.00")) and s.adjustments[0]["type"] == "preauth_advance"
    bank.preauth["PA-2026-33121"] = {"approved_amount": "20000.00", "advance_paid": "50000.00"}
    cid2, _ = await approved_case(env)
    await run_settlement(env, bank, cid2)
    assert (await st(env, cid2)).amount == Decimal("0.00")
    assert (await q(env, "SELECT detail->>'excess' AS e FROM core.settlement_task WHERE case_id = :c AND kind = 'refund_due'", c=cid2))[0].e == "38000.00"
    assert await case_status(env, cid2) == "settled"  # zero net: nothing to send to the bank


async def test_reimbursement_pays_the_member_and_missing_hospital_account_opens_a_task(env, bank):
    claim = approved_claim(claim_type="reimbursement", doc_types=["discharge_summary", "final_bill", "itemised_bill", "claim_form", "id_proof", "policy_card", "payment_receipt", "cancelled_cheque"], preauth_ref=None)
    register_claim_docs(env, claim)
    await env.sim.submit(claim)
    await jobs.drain({"fetch_documents", "start_verification"})
    cid = (await q(env, "SELECT id FROM core.claim_case WHERE hospital_claim_ref = :r", r=claim["claim_ref"]))[0].id
    await run_settlement(env, bank, cid)
    assert (await st(env, cid)).payee_type == "member"
    async with env.sm() as s, s.begin():
        await s.execute(text("UPDATE core.network_hospital SET account_hash = NULL WHERE hospital_code = 'HOSP-0001'"))
    cid3, _ = await approved_case(env)
    jobs.clear()
    out = await settlement.initiate(cid3)
    assert out["reason"] == "payee_not_configured"
    assert (await q(env, "SELECT count(*) FROM core.settlement WHERE case_id = :c", c=cid3))[0][0] == 0
    async with env.sm() as s, s.begin():
        await s.execute(text("UPDATE core.network_hospital SET account_hash = encode(sha256('SIM-ACCOUNT-HOSP-0001'::bytea), 'hex') WHERE hospital_code = 'HOSP-0001'"))


async def test_reconciliation_report_and_endpoints(env, bank):
    t0 = datetime.now(UTC).replace(microsecond=0)  # bank paid_at is truncated to whole seconds
    cid, _ = await approved_case(env)
    await run_settlement(env, bank, cid)
    for _ in range(50):  # the bank callback marks the row paid asynchronously
        if (await st(env, cid)).status == "paid":
            break
        await asyncio.sleep(0.1)
    mine = (await st(env, cid)).utr  # other tests' settlements from the same second may share the window
    assert [d for d in (await settlement.reconcile(since=t0))["diffs"] if d["utr"] == mine] == []
    bank.ledger_rows.append({"utr": "SIMUTR20261006999999", "settlement_id": str(uuid.uuid4()), "amount": "1.00", "status": "paid"})
    bank.ledger_rows[0]["amount"] = "1.00"
    kinds = {d["kind"] for d in (await settlement.reconcile(since=t0))["diffs"]}
    assert {"missing_in_db", "amount_diff"} <= kinds
    today = datetime.now(UTC).date().isoformat()
    async with env.client("admin1", ["admin"]) as cl:
        csv_text = (await cl.get(f"/v1/admin/settlements/report?date_from={today}&date_to={today}")).text
        assert csv_text.splitlines()[0].startswith("settlement_id,claim_no") and "SIMUTR" in csv_text
        assert (await cl.get("/v1/admin/settlements/reconciliation")).json()["last"]["kind"] == "settlement"
        assert (await cl.get("/v1/settlements?status=paid")).json()["items"]
        assert (await cl.get(f"/v1/cases/{cid}/settlement")).json()["settlements"][0]["status"] == "paid"
    async with env.client("reviewer1", ["reviewer"]) as cl:
        assert (await cl.get(f"/v1/admin/settlements/report?date_from={today}&date_to={today}")).status_code == 403
    async with env.svc() as sc:
        assert (await sc.get(f"/internal/settlement/{cid}/status")).json()["status"] in ("settled", "closed", "paid")
