# 03-06 — insurer-api: Settlement (Simulated) (2.2.5)

Owner: Dev B. Status: PROPOSED. Payments are **simulated** (real bank integration is out of scope).
Depends on: 04 (final decisions, event stream), 02 (outbox pattern, HMAC), 01-04 audit, tpa-sim bank-sim (04-shared-services doc 06), contract `SettlementNotice` (01-02).
Consumed by: 10 (settlement queue UI), 09 (n8n timers), 05-integration (e2e scripts).

## 1. Goal
After a final `approved` or `partially_approved` decision, simulate payment end to end:

- initiate a payout request
- receive the bank-sim callback (success or failure)
- record the UTR (unique transaction reference)
- notify the hospital
- close the case
- keep utilisation bookkeeping and reconciliation correct

Model the differences between cashless (pay the hospital) and reimbursement (pay the member, or hospital when direct settlement is flagged), plus failures, retries, reversals, duplicate callbacks and amount mismatches. TDS and GST are out of scope (simplification).

Hard guard: this project never talks to a real bank. The service refuses to start unless `INS_SETTLEMENT_MODE=sim`.

## 2. Inputs / Outputs
- Input: `claim_case` in `approved|partially_approved` (final decision exists); bank-sim callbacks; admin/senior actions (retry, reverse, release utilisation); n8n timers.
- Output: `settlement` and `settlement_event` rows; `SettlementNotice` callback to hospital (outbox); case → `settled` → `closed`; audit events; SSE events (`settlement.paid`, `settlement.failed`); payout report CSV; reconciliation results.

### 2.1 Lifecycle
```
decision.final (approve/partial) --> initiate --> settlement(initiated) --> bank-sim /bank/payouts (202)
   bank-sim callback status=paid   --> settlement(paid) --> case settled --> hospital notice --> (ack or 7 days) --> case closed
   bank-sim callback status=failed --> settlement(failed)
        retryable & attempts < 3 --> schedule retry --> initiated (attempt+1)
        terminal or attempts = 3 --> settlement_failed task for senior_reviewer (manual retry/fix)
   paid --reversal--> settlement(reversed) --> case back to approved + task --> admin action --> initiated again
```

## 3. Data model
Table `core.settlement` exists (01-insurer-db). Extend (PROPOSED migration 0011):
```sql
ALTER TABLE core.settlement
  ADD COLUMN payee_type TEXT NOT NULL CHECK (payee_type IN ('hospital','member')),
  ADD COLUMN payee_ref TEXT NOT NULL,                       -- hospital_code or member_id
  ADD COLUMN beneficiary_account_hash CHAR(64),             -- hash only; synthetic accounts, prefix SIM
  ADD COLUMN gross_amount NUMERIC(14,2) NOT NULL,           -- approved amount
  ADD COLUMN adjustments JSONB NOT NULL DEFAULT '[]',       -- [{type, amount, ref, note}]
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0,
  ADD COLUMN idempotency_key UUID NOT NULL,
  ADD COLUMN failure_reason TEXT,
  ADD COLUMN next_retry_at timestamptz,
  ADD COLUMN mode TEXT NOT NULL DEFAULT 'neft_sim' CHECK (mode IN ('neft_sim','imps_sim','rtgs_sim')),
  ADD COLUMN release_utilisation BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE core.settlement ADD CONSTRAINT ck_settlement_status
  CHECK (status IN ('initiated','paid','failed','reversed'));
CREATE UNIQUE INDEX ux_settlement_utr ON core.settlement(utr) WHERE utr IS NOT NULL;
CREATE UNIQUE INDEX ux_settlement_active_case ON core.settlement(case_id) WHERE status IN ('initiated','paid','failed');

CREATE TABLE core.settlement_event (
  id UUID PRIMARY KEY,
  settlement_id UUID NOT NULL REFERENCES core.settlement(id),
  event TEXT NOT NULL,                      -- initiated, bank_request_sent, bank_ack, paid, failed, retry_scheduled, reversed, anomaly, released_utilisation
  detail JSONB,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_settlement_event_s ON core.settlement_event(settlement_id, created_at);

CREATE TABLE core.settlement_task (          -- manual follow-up queue
  id UUID PRIMARY KEY, settlement_id UUID NOT NULL REFERENCES core.settlement(id),
  kind TEXT NOT NULL CHECK (kind IN ('settlement_failed','reversal','anomaly','refund_due')),
  status TEXT NOT NULL CHECK (status IN ('open','done','cancelled')) DEFAULT 'open',
  detail JSONB, opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz, closed_by UUID);
```

### 3.1 Status machine
| From | To | Trigger |
|---|---|---|
| (none) | `initiated` | finalize of decision or admin retry |
| `initiated` | `paid` | bank callback `paid` with matching amount |
| `initiated` | `failed` | bank callback `failed` |
| `failed` | `initiated` | retry (auto if retryable and attempts < 3, else senior retry) |
| `paid` | `reversed` | bank reversal callback or admin simulate-reverse |
| `reversed` | `initiated` | admin action after reversal task |
Case transitions: `approved|partially_approved → settled` on `paid`; `settled → closed` on hospital ack or after `INS_AUTOCLOSE_DAYS` (7); reversal moves the case back to `approved|partially_approved` (via the allowed reverse edge, PROPOSED addition to the insurer state machine in 01-02: `settled → approved|partially_approved` only through the reversal service).

### 3.2 Payee resolution (PROPOSED)
| Claim type | Payee | Notes |
|---|---|---|
| cashless | `hospital` (network hospital account) | the hospital must exist in `network_hospital` with `account_hash` |
| reimbursement | `member` | member account from policy record (synthetic) |
| reimbursement with `payment_preference=hospital` | `hospital` | requires optional contract field (v1.1 proposal); until then reimbursement → member |

### 3.3 Adjustments
`adjustments[]` entries: `preauth_advance` (simulated advance from tpa-sim pre-auth record, subtracted), `co_pay_collected_by_hospital` (informational), `prior_partial_payment` (subtracted). Net payout = `max(0, gross_amount - sum(subtractive adjustments))`. If subtractive adjustments exceed gross, net = 0 and a `settlement_task(kind='refund_due')` is opened with the excess (flag only; no money movement).

## 4. API / endpoints
| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/v1/cases/{id}/settlement` | reviewer+ | status, adjustments, events, tasks |
| GET | `/v1/settlements` | reviewer+ / admin | list with filters `status`, `date_from`, `date_to`, `payee_type`, paging |
| POST | `/internal/cases/{id}/settlement/initiate` | internal (n8n or finalize) | idempotent |
| POST | `/internal/settlement/bank-callback` | tpa-sim (HMAC key `bank-sim`) | `{utr, settlement_id, status, amount, reason?, paid_at}` |
| POST | `/v1/settlements/{id}/retry` | senior_reviewer | only from `failed` |
| POST | `/v1/settlements/{id}/reverse` | senior_reviewer | simulate reversal (demo/testing; env flag) |
| POST | `/v1/settlements/{id}/release-utilisation` | senior_reviewer | credit back sum-insured utilisation after reversal (audited, reason) |
| GET | `/v1/settlement-tasks` | senior_reviewer | open tasks queue |
| POST | `/v1/settlement-tasks/{id}/close` | senior_reviewer | `{note}` |
| GET | `/v1/admin/settlements/report` | admin | CSV by date range, IST presentation |
| GET | `/v1/admin/settlements/reconciliation` | admin | last reconciliation result |

### 4.1 Hospital callback `/v1/insurer-callbacks/settlements`
```json
{"claim_ref":"HC-2026-000045","insurer_claim_no":"IC-2026-000123","amount":"84250.00",
 "utr":"SIMUTR20261006000045","paid_at":"2026-10-06T14:20:00Z","mode":"neft_sim",
 "status":"paid","sequence":9}
```
Reversal notice: same shape with `"status":"reversed"` and the original UTR. Failure is **not** pushed to the hospital in v1 (internal problem); the hospital sees status `approved` until paid (PROPOSED).

### 4.2 Bank-sim interface (lives in `tpa-sim`, owner Dev B; see 04-shared-services/06-tpa-sim.md)
Request the insurer sends:
```json
POST {INS_TPA_SIM_URL}/bank/payouts
X-Key-Id: ins-bank-001  X-Signature: ...  X-Idempotency-Key: <settlement.idempotency_key>
{"settlement_id":"c4d1...","amount":"84250.00","payee_ref":"HOSP-APOLLO-SIM","payee_account_hash":"ab12...","mode":"neft_sim"}
```
Response `202 {"payout_id":"...","status":"queued"}`. Later the sim posts to `/internal/settlement/bank-callback`:
```json
{"settlement_id":"c4d1...","utr":"SIMUTR20261006000045","status":"paid","amount":"84250.00","paid_at":"2026-10-06T14:20:00Z"}
{"settlement_id":"c4d1...","status":"failed","reason":"bank_down","amount":"84250.00"}
```
Sim profiles (scripted per payee or via header `X-Sim-Profile`): `always_pay`, `fail_once_then_pay`, `always_fail`, `reverse_after_pay`, `duplicate_callback`, `amount_mismatch`, `slow_5min`.

Ledger endpoint for reconciliation: `GET {sim}/bank/ledger?from=&to=` → `[{utr, settlement_id, amount, status, paid_at}]`.

### 4.3 Failure reason mapping
| Reason | Retryable | Action |
|---|---|---|
| `bank_down` | yes | backoff retry (5 min × attempt) |
| `timeout` | yes | retry |
| `limit_exceeded` | no | task `settlement_failed` (split payment not supported in v1) |
| `invalid_account` | no | task; fix payee data then retry |
| `account_frozen` | no | task |
| unknown | no | task + audit `settlement.anomaly` |

### 4.4 Events published (04 section 4.4 stream)
`settlement.paid {utr}`, `settlement.failed {reason, attempt}`, `settlement.reversed`, `delivery.dead_letter` (outbox). Payload never includes account data.

## 5. Build tasks
Paths relative to `insurer/api/app/`.
1. Migration `alembic/versions/0011_settlement.py` (section 3) plus round-trip test.
2. `services/settlement.py`: `initiate`, `on_bank_callback`, `retry`, `reverse`, `release_utilisation`, `close_after_ack`.
3. `services/payee.py`: `resolve_payee(case)` per 3.2, refuses if the hospital has no `account_hash`.
4. `services/adjustments.py`: pre-auth advance lookup from tpa-sim (`GET {sim}/preauth/{ref}`), prior payments; net amount calculator (Decimal, ROUND_HALF_UP).
5. `clients/bank_sim.py`: HMAC signing via `claim_contract.signing`, idempotency key, timeouts, mapping of errors to retryable/terminal.
6. `api/internal_settlement.py`: bank callback endpoint with HMAC verification, replay safety (UNIQUE `utr`), amount verification, race handling (409 when settlement row not yet committed so the sim retries).
7. Case transitions via `cases.transition` and `assert_transition`; hospital notification via outbox `settlements` with sequence.
8. Retry scheduler `jobs/settlement_retry.py` (runs every minute, selects `status='failed' AND next_retry_at <= now()`); failure mapping per 4.3; terminal path opens `settlement_task`.
9. Auto-close job `jobs/settlement_autoclose.py`: `settled` cases with hospital ack or older than `INS_AUTOCLOSE_DAYS` → `closed`. Hospital ack is the `acked_at` on the outbox row for the settlement notice.
10. Payout report generator `services/reports.py` (CSV with IST timestamps) and endpoint.
11. Reconciliation job `jobs/settlement_reconcile.py` (nightly): compare `settlement` against bank-sim ledger; mismatch kinds `missing_in_ledger`, `missing_in_db`, `amount_diff`, `status_diff`; results saved, admin alert event.
12. Utilisation reconciliation job: `core.policy_claim_utilisation` equals sum(settled) + sum(approved-unsettled); writes audit `reconcile.utilisation` and flags drift.
13. `api/settlement.py` router for 4 with RBAC and filters.
14. Simulation guard in `config.py`: startup fails when `INS_SETTLEMENT_MODE != "sim"`; validator for synthetic accounts (must start with `SIM`).
15. Audit events: `settlement.initiated`, `settlement.paid`, `settlement.failed`, `settlement.retry_scheduled`, `settlement.reversed`, `settlement.anomaly`, `settlement.utilisation_released`.
16. Metrics: payout success rate, time initiate → paid, retries per settlement, open tasks by kind.
17. Tests (section 9), seeds `seeds/settlement_demo.py`, tpa-sim profile fixtures.

## 6. Key logic / pseudocode

### 6.1 Initiate
```python
async def initiate(case_id):
    async with advisory_lock(case_id):
        case = await cases.get(case_id)
        assert case.status in ("approved", "partially_approved")
        existing = await settlements.active_for_case(case_id)
        if existing:                                       # idempotent
            return existing
        if case.approved_amount == Decimal("0"):
            return await close_without_payment(case)       # nothing to pay
        adj = await adjustments.for_case(case)             # pre-auth advance, prior payments
        net = max(Decimal("0"), case.approved_amount - sum(a.amount for a in adj if a.subtractive))
        payee = resolve_payee(case)                        # may raise PayeeNotConfigured
        s = Settlement(case_id=case_id, gross_amount=case.approved_amount, amount=net, adjustments=adj,
                       payee_type=payee.type, payee_ref=payee.ref,
                       beneficiary_account_hash=payee.account_hash, status="initiated",
                       initiated_at=now(), idempotency_key=uuid4(), mode=settings.default_mode)
        await settlements.add(s)
        await settlement_events.add(s.id, "initiated")
        await audit.append(case_id, "settlement.initiated", payload=redact(summary(s)))
        if net == 0 and excess := excess_over_gross(adj, case.approved_amount):
            await tasks.open(s.id, "refund_due", {"excess": str(excess)})
    await send_to_bank(s)                                   # outside lock

async def send_to_bank(s):
    try:
        await bank.payout(s)                                # 202 expected
        await settlement_events.add(s.id, "bank_request_sent")
    except RetryableBankError as e:
        await schedule_retry(s, reason=str(e))
    except TerminalBankError as e:
        await fail_terminal(s, reason=str(e))
```
`PayeeNotConfigured` opens a `settlement_failed` task and does not create a settlement row; the decision stays final.

### 6.2 Bank callback
```python
async def on_bank_callback(cb):
    s = await settlements.by_id(cb.settlement_id)
    if s is None:
        raise Conflict("settlement_not_committed")  # sim retries
    if s.status == "paid" and s.utr == cb.utr:
        return Replay()
    if s.status == "paid" and cb.utr != s.utr:
        return await anomaly(s, "duplicate_callback_different_utr")
    if cb.status == "paid":
        if Decimal(cb.amount) != s.amount:
            return await anomaly(s, "amount_mismatch", cb)
        s.status, s.utr, s.paid_at = "paid", cb.utr, cb.paid_at
        await save(s)
        await cases.transition(s.case_id, "settled")
        await outbox.enqueue(s.case_id, "settlements", notice(s))
        await events.publish(
            "settlement.paid", s.case_id, {"utr": s.utr}, audience=["reviewer", "admin"]
        )
        await audit.append(s.case_id, "settlement.paid", ...)
    elif cb.status == "failed":
        s.attempt_count += 1
        s.status = "failed"
        s.failure_reason = cb.reason
        if is_retryable(cb.reason) and s.attempt_count < settings.max_attempts:
            s.next_retry_at = now() + minutes(5 * s.attempt_count)
            await settlement_events.add(s.id, "retry_scheduled", {"at": s.next_retry_at})
        else:
            await tasks.open(
                s.id, "settlement_failed", {"reason": cb.reason, "attempts": s.attempt_count}
            )
        await save(s)
    elif cb.status == "reversed":
        await reverse(s, source="bank")
```
UTR format (PROPOSED): `SIMUTR` + `YYYYMMDD` + 6-digit sequence from the sim. Insurer treats UTR as opaque but validates the `SIMUTR` prefix in sim mode.

### 6.3 Reversal
```python
async def reverse(s, source):
    s.status = "reversed"
    await save(s)
    await cases.reverse_settled(
        s.case_id
    )  # settled -> approved|partially_approved via explicit service
    await outbox.enqueue(s.case_id, "settlements", notice(s, status="reversed"))
    await tasks.open(s.id, "reversal", {"source": source})
    await audit.append(s.case_id, "settlement.reversed", ...)
```
After a reversal a senior can retry (a new initiate with a fresh idempotency key; the old row keeps history through `settlement_event`; partial unique index frees because status `reversed` is excluded) and optionally release utilisation.

### 6.4 Retry timing
`next_retry_at = now + 5 min × attempt` (5, 10 min), max attempts 3, then task. Manual senior retry resets `attempt_count` only with a note.

### 6.5 Reconciliation
```python
async def reconcile(day):
    ours = await settlements.paid_between(day)
    theirs = await bank_sim.ledger(day)
    diffs = diff_by_utr(ours, theirs)  # missing_in_ledger, missing_in_db, amount_diff, status_diff
    await reconciliation_runs.save(day, diffs)
    if diffs:
        await events.publish(
            "settlement.reconcile_mismatch", None, {"count": len(diffs)}, audience=["admin"]
        )
```
Utilisation reconcile: `util = Σ settled + Σ approved-unsettled`; compare to `policy_claim_utilisation.used`; drift is audited, never auto-corrected.

## 7. Config / env vars
| Name | Default | Notes |
|---|---|---|
| `INS_TPA_SIM_URL` | `http://tpa-sim:8500` | bank-sim base |
| `INS_BANK_SIM_HMAC_SECRET` | none | per-direction secret (01-05 conventions) |
| `INS_SETTLEMENT_MODE` | `sim` | any other value refuses to start |
| `INS_SETTLEMENT_MAX_ATTEMPTS` | `3` | |
| `INS_SETTLEMENT_RETRY_BASE_MINUTES` | `5` | |
| `INS_AUTOCLOSE_DAYS` | `7` | |
| `INS_ALLOW_SIMULATED_REVERSAL` | `true` in dev | gates `/reverse` endpoint |
| `INS_REPORT_TZ` | `Asia/Kolkata` | presentation only |

## 8. Error handling and edge cases
| Situation | Behaviour |
|---|---|
| Approved amount 0 | No settlement; case goes to `closed` after decision callback is delivered |
| Bank callback before initiate commit (race) | `409 settlement_not_committed`; sim retries |
| Duplicate callback, same UTR | Replay safe, no second notice |
| Duplicate callback, different UTR | Ignore second, audit `settlement.anomaly`, open `anomaly` task |
| Amount mismatch (partial payment) | Treated as anomaly, status unchanged, task opened |
| Reversal after hospital already notified | Status `reversed`, new notice `status=reversed`, case back to approved, `reversal` task |
| Utilisation on reversal | Not credited back automatically; senior can `release-utilisation` (audited with reason) |
| Pre-auth advance greater than approved | Net 0 and `refund_due` task (flag only) |
| Payee missing account | `PayeeNotConfigured`, task opened, decision remains final; fixing master data then `retry` |
| Sim down at initiate | Retryable; settlement stays `initiated`, scheduler re-sends with the same idempotency key |
| Two initiate calls at once | Advisory lock plus partial unique index → one settlement |
| Hospital notice delivery fails | Outbox retry (01-01 section 8); dead letter alert; status of settlement unaffected |
| Synthetic-only guard | Account strings must start with `SIM`; unit test fails if a seed looks real; startup refuses non-sim mode |
| Time zones | stored UTC; reports show IST |
| Case withdrawn after approval | Not allowed once final; settlement continues |
| Retry endpoint called on non-failed settlement | `409 invalid_state` |

## 9. Tests
### 9.1 Unit
- Adjustment maths (parametrised): gross 84,250 with advance 20,000 → net 64,250; advance 90,000 → net 0 and `refund_due` 5,750; no adjustments.
- Payee resolution: cashless, reimbursement, hospital preference, missing account.
- UTR validation, failure reason mapping, retry delay schedule (5, 10).
- Report CSV: columns, IST formatting, decimal formatting.

### 9.2 Integration with tpa-sim profiles (table)
| ID | Profile | Expected |
|---|---|---|
| S1 | `always_pay` | settled, notice sent once, closed after ack |
| S2 | `fail_once_then_pay` | failed, retry scheduled, then paid |
| S3 | `always_fail` (`bank_down`) | 3 attempts then `settlement_failed` task |
| S4 | `always_fail` (`invalid_account`) | immediate task, no retries |
| S5 | `reverse_after_pay` | reversed, case back to approved, reversal task, hospital reversal notice |
| S6 | `duplicate_callback` | one notice only |
| S7 | `amount_mismatch` | anomaly task, not paid |
| S8 | `slow_5min` | stays initiated, then paid; no duplicate payout request |

### 9.3 Other
- Idempotency of initiate; concurrency (two initiates → one settlement).
- Reconciliation detects each injected mismatch kind and is clean on a clean run.
- Contract test: `SettlementNotice` validates against `claim_contract.models`.
- Mode guard: starting with `INS_SETTLEMENT_MODE=live` exits non-zero.
- Audit chain verifies after S1-S8.
- RBAC: reviewer cannot retry or reverse; admin cannot approve decisions.

## 10. Acceptance criteria
- [ ] Cashless and reimbursement flows reach `settled` and `closed` in the e2e script.
- [ ] Failure, retry and terminal paths produce the expected tasks and audit events (S2-S4).
- [ ] Hospital notice delivered exactly once (idempotent) and sequence-ordered, including the reversal notice.
- [ ] Reconciliation reports zero mismatches on a clean run and flags every injected mismatch.
- [ ] The service refuses to start in a non-sim mode.
- [ ] SSE settlement events reach reviewer and admin audiences only.

## 11. Dependencies
04 (finalize triggers initiate, event stream), 02 (outbox, HMAC), tpa-sim bank endpoints (`/bank/payouts`, `/bank/ledger`, `/preauth/{ref}`; Dev B owns), 10 (settlement queue and task UI), 01-04 audit.

## 12. Claude Code kickoff prompt
> Read docs/implementation/03-dev-B-insurer/06-api-settlement.md and the shared-contract docs. Plan, then implement tasks 1-17 using the tpa-sim bank profiles for tests (S1-S8). Keep everything simulated: refuse non-sim modes and enforce the `SIM` account prefix. Report against section 10.
