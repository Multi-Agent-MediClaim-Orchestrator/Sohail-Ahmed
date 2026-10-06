# 03-04 — insurer-api: Decision Gate, Approvals, Final Decisions, Event Stream (2.2.3)

Owner: Dev B. Status: PROPOSED.
Depends on: 03 (recommendation, findings), 07 (calc engine), 01-03 config (thresholds), 01-04 audit, 01-02 models/state machines.
Consumed by: 05 (round outcome feeds the gate), 06 (settlement starts after final), 09 (n8n flows), 10 (UI queues and SSE).

## 1. Goal
Turn a recommendation into a binding decision with correct human authority:

- **Auto-approve (user decision):** when ALL hard gates pass (identity ≥ `identity_min_score`, authenticity ≥ floor, completeness, calculation consistent, no exclusion/waiting-period/high-risk flag) and the payable amount is ≤ `T_auto`, the system approves directly (actor=`system`, audit `decision.auto_approved`, case goes `ready_for_decision` → `approved`/`partially_approved`). No human click is needed.
- Above `T_auto` (up to `T_four`) the agent asks one human (Approver) for the final-stage decision. Any failed gate or `review_required` flag forces human review even below `T_auto`. Rejections are never automatic.
- Two distinct humans decide above `T_four`, at least one of them a `senior_reviewer`.
- `T_auto` is a starting value (PROPOSED 50,000 INR) and is re-tuned on the synthetic evaluation set so the false auto-approve rate stays below ~1% (02-evaluation-harness).
- Produce the final `Decision`, transition the case, send it to the hospital, and trigger settlement.
- Own the **insurer event stream** (`/v1/events/stream`, SSE) that the UI (10) and n8n (09) subscribe to. It is defined here (section 4.4) because decision and approval events are its main producers, but 05 and 06 publish into it too.

Non-goals: calculating money (07), raising queries (05), moving money (06), UI rendering (10).

## 2. Inputs / Outputs
- Input: case in `ready_for_decision` with a `decision(kind='recommendation')` and `calculation_result`; reviewer/approver actions via REST; threshold config (`thresholds` domain, 01-03).
- Output:
  - `decision(kind='final')` and `approval` rows
  - case status `approved|partially_approved|rejected` (or back to `ready_for_decision` on return)
  - hospital callback `/v1/insurer-callbacks/decisions` (via outbox, 01-01 section 8)
  - audit events (`decision.recommended`, `human.approved`, `human.rejected`)
  - Redis events consumed by the SSE endpoint
  - settlement initiation (06)

### 2.1 Where this sits in the case lifecycle
```
verifying ──(03 finalize run)──> ready_for_decision
   ready_for_decision --system auto_decide (all gates pass, payable <= T_auto, outcome approve|partial)--> [tier=auto] --> final (approved|partially_approved)
   ready_for_decision --reviewer submit (gate failed / flag / > T_auto / reject)--> awaiting_approval [tier=single_approver | dual_approver]
   awaiting_approval --approver approve (n of n)--> final
   awaiting_approval --approver return-->  ready_for_decision   (task status=returned)
   awaiting_approval --approver reject-->  ready_for_decision   (PROPOSED: single reject sends back, never auto-final reject)
final approved/partially_approved --> 06 settlement
```
All transitions go through `claim_contract.assert_transition` (01-02 section 4.2); the only writer of `claim_case.status` is `services/cases.py::transition`.

## 3. Data model
Existing tables (see 01-insurer-db): `core.decision`, `core.approval`, `core.claim_case`, `core.calculation_result`, `config.*`. This doc adds (PROPOSED, migration 0009):

```sql
-- 0009_decision_gate.sql
CREATE TABLE core.decision_task (
  id UUID PRIMARY KEY,
  case_id UUID NOT NULL REFERENCES core.claim_case(id),
  decision_id UUID NOT NULL REFERENCES core.decision(id),
  tier TEXT NOT NULL CHECK (tier IN ('auto','single_approver','dual_approver')),
  required_approvals SMALLINT NOT NULL CHECK (required_approvals IN (1,2)),
  min_senior SMALLINT NOT NULL DEFAULT 0 CHECK (min_senior IN (0,1)),
  allowed_roles TEXT[] NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','completed','returned','cancelled')),
  threshold_snapshot JSONB NOT NULL,          -- {t_auto, t_four, thresholds_v, gate_amount, flags[]}
  opened_by UUID NOT NULL,                    -- reviewer sub
  opened_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz,
  close_reason TEXT
);
CREATE INDEX ix_decision_task_queue ON core.decision_task(status, tier, opened_at);
CREATE UNIQUE INDEX ux_decision_task_one_open ON core.decision_task(case_id) WHERE status = 'open';

ALTER TABLE core.approval
  ADD COLUMN task_id UUID REFERENCES core.decision_task(id),
  ADD COLUMN valid BOOLEAN NOT NULL DEFAULT true,       -- false when invalidated by a later reject/return
  ADD COLUMN role TEXT NOT NULL;                        -- role used when voting (snapshot)
CREATE UNIQUE INDEX ux_approval_one_vote ON core.approval(task_id, approver);

ALTER TABLE core.decision
  ADD COLUMN supersedes UUID REFERENCES core.decision(id),   -- reserved for corrections (6.4)
  ADD COLUMN revision SMALLINT NOT NULL DEFAULT 1,
  ADD COLUMN override_reason TEXT,
  ADD COLUMN gate_amount NUMERIC(14,2);
```

### 3.1 Gate tiers (PROPOSED defaults: `T_auto` = 50,000 INR, `T_four` = 5,00,000 INR)
| Gate amount G | Flags | Tier | Who must act |
|---|---|---|---|
| G ≤ T_auto, outcome approve/partial | all hard gates pass and no `review_required` flag | `auto` | nobody: system auto-approves (`decision.auto_approved`) |
| T_auto < G ≤ T_four | any | `single_approver` | one Approver or Senior Reviewer |
| any G ≤ T_four | ≥1 `review_required` or a failed hard gate, or outcome = reject | `single_approver` | one Approver or Senior Reviewer (the agent asks for the final-stage decision) |
| G > T_four | any | `dual_approver` | two distinct people, ≥1 `senior_reviewer` |
| outcome = reject and claimed > T_four | any | `dual_approver` | same as above |

`gate_amount = max(claimed_amount, recommended_payable)`. Using the larger prevents a big claim being trimmed by the engine into a cheaper tier, and prevents a rejection of a large claim from skipping oversight.

`review_required` flag catalogue (config domain `thresholds.review_flags`, each toggleable):

| Flag | Raised when |
|---|---|
| `overridden_blocker` | a reviewer overrode a blocker finding (03) |
| `agent_deterministic_disagreement` | an agent verdict conflicts with the code check (03 supervisor) |
| `degraded_mode` | any verification step ran with fallback (LLM down, OCR only) |
| `watchlist_hospital` | hospital is on the watch list (admin config) |
| `high_utilisation` | claim would push utilisation above 80% of remaining sum insured |
| `fraud_warning` | authenticity step score below warn floor (not blocker) |
| `manual_increase` | reviewer raised amount above calc result (only senior; 4.2) |
| `escalated_case` | case came through round-3 escalation (05) |

### 3.2 Worked tier examples
| Claimed | Payable | Outcome | Flags | G | Tier |
|---|---|---|---|---|---|
| 42,000 | 38,500 | approve | none | 42,000 | auto (system approves) |
| 50,000 | 50,000 | approve | none | 50,000 | auto (equality stays low) |
| 50,000.01 | 40,000 | partial | none | 50,000.01 | single_approver |
| 80,000 | 20,000 | partial | none | 80,000 | single_approver (trimmed, still gated by claimed) |
| 30,000 | 30,000 | approve | `fraud_warning` | 30,000 | single_approver |
| 6,20,000 | 0 | reject | none | 6,20,000 | dual_approver (reject of large claim) |
| 5,00,000 | 4,20,000 | partial | none | 5,00,000 | single_approver (equality) |
| 5,00,001 | 4,20,000 | partial | none | 5,00,001 | dual_approver |

## 4. API / endpoints
All under insurer-api (8100), Keycloak `insurer` realm roles: `reviewer`, `approver`, `senior_reviewer`, `admin`. Responses use RFC 7807 (01-01 section 7). Mutating calls require `If-Match: <etag>` (the case ETag) to avoid lost updates.

### 4.1 Decision endpoints
| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/internal/cases/{id}/decision/auto-decide` | system (n8n / insurer-api) | evaluates `compute_gate`; if tier is `auto` creates the final decision with actor=system, else opens the approval task. Idempotent |
| POST | `/v1/cases/{id}/decision/recommend` | system / reviewer | recompute recommendation from the latest verification run |
| GET | `/v1/cases/{id}/decision` | reviewer+ | current recommendation, open task, votes, gate explanation |
| POST | `/v1/cases/{id}/decision/submit` | reviewer | for cases the gate did not auto-approve: confirm or edit the recommendation; creates `decision` and `decision_task` for an approver |
| POST | `/v1/decisions/{decision_id}/approvals` | approver / senior_reviewer | vote: `approve`, `reject`, `return` |
| GET | `/v1/approvals/queue` | approver / senior_reviewer | tasks awaiting me (excludes ones I touched or am blocked on by SoD) |
| POST | `/v1/cases/{id}/decision/withdraw-task` | senior_reviewer | cancel the open task with a reason |
| GET | `/v1/admin/gate/stats` | admin | cases per tier, average time per tier, thresholds history |
| POST | `/internal/cases/{id}/decision/finalize-callback` | internal (n8n) | flow notifies completion; idempotent |

### 4.2 Submit request, success and failure examples
Reviewer edits an amount downwards:
```json
POST /v1/cases/6f0d.../decision/submit
If-Match: "case-v17"
{
  "outcome": "partial",
  "approved_amount": "84250.00",
  "reason_codes": ["room_rent_cap", "co_pay"],
  "deduction_edits": [
    {"line_ref": "LINE-7", "rule_id": "manual", "amount": "1200.00",
     "explanation": "Non-medical consumables per policy annexure 3"}
  ],
  "note": "Reduced consumables per annexure"
}
```
Response `200`:
```json
{
  "decision_id": "a1c2...",
  "kind": "pending_approval",
  "gate": {"tier": "single_approver", "required_approvals": 1, "gate_amount": "112400.00",
           "flags": [], "thresholds_v": 4, "explanation": "gate_amount 112400.00 > T_auto 50000.00"},
  "case_status": "awaiting_approval",
  "task_id": "9be1..."
}
```
Auto-tier case (`G ≤ T_auto`, all gates pass, no flags): `auto-decide` returns `kind: "final"`, `decided_by: "system"` and `case_status: "partially_approved"` (or `approved`) straight away, and audit `decision.auto_approved` is written. A reviewer cannot submit for such a case (`409 already_decided`).

Rejected for raising the amount above the engine result:
```json
HTTP 422
{"type":"https://claims.local/errors/exceeds_calculation","title":"Amount exceeds calculation",
 "status":422,"code":"exceeds_calculation",
 "detail":"approved_amount 91000.00 exceeds engine payable 84250.00; senior_reviewer with override_reason required",
 "errors":[{"field":"approved_amount","message":"max 84250.00 for role reviewer"}],"trace_id":"..."}
```
Validation rules on submit (all 422 unless stated):
- `outcome` in `approve|partial|reject`; `needs_info` is not accepted here (use 05).
- `approve`/`partial` need `approved_amount > 0` and ≤ `calc.payable` (reviewer) or ≤ `calc.payable` plus override (senior).
- `reject` needs ≥1 code from `policy_rules.reject_reasons`; `approved_amount` must be absent or `0.00`.
- Every manual decrease needs `reason_codes` entry; every `deduction_edits[i].amount` is a positive Decimal string, 2dp.
- Sum of deductions + approved amount must reconcile with `claimed_amount` within 0.01 or `422 amount_not_reconciled`.
- Case must be in `ready_for_decision` (else `409 invalid_transition`); a stale `If-Match` returns `412`.

### 4.3 Approval vote
```json
POST /v1/decisions/a1c2.../approvals
{"verdict": "approve", "comment": "Checked cap logic and annexure"}
```
Responses:
- `200 {"status":"pending","remaining":1}` when more votes are needed.
- `200 {"status":"final","outcome":"partial","case_status":"partially_approved"}` when the last needed vote arrives.
- `200 {"status":"returned"}` for `return` or `reject`.
- `403 sod_violation` (own work), `403 role_not_allowed`, `409 already_voted`, `409 task_closed`, `422 comment_required` (return/reject need a comment).

### 4.4 Insurer event stream: `GET /v1/events/stream` (Server-Sent Events)
Defined here for the whole insurer-api; 05 and 06 publish into it. This is the endpoint `10-ui.md` relies on.

Connection:
```
GET /v1/events/stream?topics=cases,queues,settlement&case_id=<optional>
Authorization: Bearer <access token>        (or BFF session cookie; EventSource cannot set headers, so the BFF proxies and injects the token)
Accept: text/event-stream
Last-Event-ID: <optional>
```
Query params: `topics` (comma list, default all the caller may see), `case_id` (restrict to one case; used by the review workspace). Roles filter automatically: reviewers see `cases` and `queries`; approvers add `approvals`; senior adds `escalations`; admin adds `admin`.

Frame format (one JSON object per `data:` line; `id` is a Redis stream id, so `Last-Event-ID` replay works for the last 1,000 events or 15 minutes):
```
id: 1728210000123-0
event: approval.requested
data: {"event_id":"1728210000123-0","type":"approval.requested","ts":"2026-10-06T10:15:30Z",
       "case_id":"6f0d...","insurer_claim_no":"IC-2026-000123","payload":{"tier":"single_approver","task_id":"9be1...","gate_amount":"112400.00"},
       "audience":["approver","senior_reviewer"]}

: heartbeat
```
Heartbeat comment line every 15 s. Client retry hint `retry: 3000`.

Event catalogue (producer in brackets):
| Type | Producer | Audience | Payload highlights |
|---|---|---|---|
| `case.received` | 02 | reviewer | claim_ref, claim_type |
| `case.status_changed` | cases service | all | from, to, reason |
| `verification.step_completed` | 03 | reviewer | step, result, blocker_count |
| `case.ready_for_decision` | 03 | reviewer | recommendation outcome, payable |
| `decision.recommended` | this doc | reviewer | outcome, payable, gate preview |
| `approval.requested` | this doc | approver, senior | tier, task_id, gate_amount |
| `approval.voted` | this doc | reviewer, approver | voter role (no names for non-admin), verdict, remaining |
| `decision.final` | this doc | all | outcome, approved_amount |
| `query.draft_ready` | 05 | reviewer | query_id, round |
| `query.sent` / `query.answered` / `query.overdue` | 05 | reviewer | query_id, round |
| `escalation.raised` | 05 | senior | reason code |
| `settlement.paid` / `settlement.failed` | 06 | reviewer, admin | utr (paid), reason (failed) |
| `delivery.dead_letter` | outbox | admin | callback type, attempts |
| `config.published` | config service | admin | domain, version |

Implementation notes:
- Redis stream `ins:events` (XADD, MAXLEN ~ 10000); the endpoint reads with XREAD BLOCK per connection (or a single reader fan-out via an in-process broadcaster; PROPOSED: fan-out).
- Authorisation is applied **per event** before writing a frame (audience list plus case access rules), not only at connect time.
- `INS_SSE_MAX_CONNECTIONS_PER_USER=5`; the sixth connection gets `429 too_many_streams`.
- Payloads carry **no PII** (masked names only, no ID numbers) per 05-integration privacy doc.
- If Redis is unavailable the endpoint returns `503` and the UI falls back to 15 s polling of `/v1/cases?updated_since=`.

## 5. Build tasks
Numbered, in order; file paths relative to `insurer/api/app/`.
1. `services/gate.py`: pure `compute_gate(case, recommendation, thresholds, flags) -> Gate` plus `explain_gate()` (string for UI). No I/O.
2. `services/flags.py`: `collect_review_flags(case, findings, run, thresholds)` reading the flag catalogue; unit-tested per flag.
3. Migration `alembic/versions/0009_decision_gate.py` (SQL in section 3); round-trip test (upgrade, downgrade, upgrade).
4. `services/approval_rules.py`: distinctness, role requirements, segregation of duties (approver ≠ reviewer/assignee when `INS_SEGREGATION_OF_DUTIES=true`), senior requirement for dual.
5. `services/decision.py`: `create_recommendation`, `submit_decision`, `record_approval`, `close_task`, `finalize`.
6. `api/decision.py`: routers for 4.1 with RBAC dependencies and ETag handling (`If-Match` compare to `claim_case.version`).
7. `services/decision_builder.py`: assemble `claim_contract.models.Decision` including config stamps `{policy_rules_v, thresholds_v, query_policy_v, calc_engine_version, doc_requirements_v}`.
8. Hospital callback enqueue: `outbox.enqueue(case_id, "decisions", payload)` with per-claim `sequence` (01-01 section 8).
9. State transitions only via `cases.transition()` which calls `assert_transition`. The auto tier goes straight to final (actor=system); other tiers go `ready_for_decision → awaiting_approval`.
10. Utilisation update (01-insurer-db section 6.3): `utilisation.apply()` inside the same transaction as the final decision; on failure convert to escalation with reason `sum_insured_exhausted` and flag `review_required`.
11. Return flow: `return` closes the task `returned`, marks votes `valid=false`, sets case back to `ready_for_decision` with a mandatory comment visible to the reviewer.
12. Reject flow: structured reason codes from `policy_rules.reject_reasons`; `services/reject_text.py` generates hospital-facing explanation from a template per code (the crew may polish text, a human always confirms before it is sent).
13. `api/events.py`: SSE endpoint (4.4), `services/events.py` (`publish(type, case_id, payload, audience)` writing to Redis stream, applying `redact()` from 01-04).
14. Event publishers wired in: `decision.recommended`, `approval.requested`, `approval.voted`, `decision.final`; helper exported for 05 and 06.
15. Admin gate stats endpoint (`GET /v1/admin/gate/stats`), SQL view `core.v_gate_stats`.
16. Metrics: counters for decisions per tier/outcome, histogram time-to-decision, gauge open tasks per tier.
17. Tests (section 9) and a seeded demo dataset `seeds/gate_demo.py` that creates one case per tier.
18. Update `10-ui.md` dependency list (SSE now exists) and the OpenAPI file `insurer/api/openapi.yaml`.

## 6. Key logic / pseudocode

### 6.1 compute_gate (pure)
```python
@dataclass(frozen=True)
class Gate:
    tier: Literal["auto", "single_approver", "dual_approver"]
    required: int
    roles: tuple[str, ...]
    min_senior: int
    gate_amount: Decimal


def compute_gate(case, rec, th, flags, gates_pass: bool) -> Gate:
    claimed = case.claimed_amount
    payable = rec.payable_amount or Decimal("0")
    amount = max(claimed, payable)
    big_reject = rec.outcome == "reject" and claimed > th.t_four
    if amount > th.t_four or big_reject:
        return Gate("dual_approver", 2, ("approver", "senior_reviewer"), 1, amount)
    if amount > th.t_auto or flags.review_required or not gates_pass or rec.outcome not in ("approve", "partial"):
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount)
    if not settings.auto_approve_enabled:  # INS_AUTO_APPROVE_ENABLED=true (kill switch: false forces a human)
        return Gate("single_approver", 1, ("approver", "senior_reviewer"), 0, amount)
    return Gate("auto", 0, (), 0, amount)  # system decides; actor=system
```
Edge: equality at a threshold stays in the lower tier (strict `>`). The function never reads the clock, DB or env except through arguments (so property tests can fuzz it).

### 6.2 submit_decision
```python
async def submit_decision(case_id, user, body, etag):
    async with advisory_lock(case_id):
        case = await cases.get_for_update(case_id)
        check_etag(case, etag)
        assert case.status == "ready_for_decision"
        rec = await decisions.latest_recommendation(case_id)
        calc = await calculations.get(rec.calc_result_id)
        validate_edit(body, calc, user)                       # section 4.2 rules
        flags = collect_review_flags(case, rec, user, body)   # includes manual_increase if applicable
        th = await config.resolve("thresholds", at=now())
        gate = compute_gate(case, as_rec(body), th, flags, gates_pass=all_hard_gates_pass(case))
        d = await decisions.create(case_id, kind="pending_approval" if gate.tier != "auto" else "final",
                                   outcome=body.outcome, approved_amount=body.approved_amount, ...,
                                   created_by=user.sub, gate_amount=gate.gate_amount,
                                   config_versions=stamp(th, calc))
        await audit.append(case_id, "decision.recommended", actor=user, payload=redact(summary(d)))
        if gate.tier == "auto":   # normally reached via auto_decide (system actor), see below
            await audit.append(case_id, "decision.auto_approved", actor=SYSTEM, payload=redact(summary(d)))
            return await finalize(task=None, decision=d, approvers=[])
        task = await tasks.open(case_id, d.id, gate, snapshot={...})
        await cases.transition(case_id, "awaiting_approval")
        await events.publish("approval.requested", case_id, {...}, audience=["approver", "senior_reviewer"])
        return Submitted(d, task, gate)
```

### 6.2a auto_decide (system path, runs right after verification finalises)
```python
async def auto_decide(case_id):
    async with advisory_lock(case_id):
        case = await cases.get_for_update(case_id)
        if case.status != "ready_for_decision": return AlreadyDecided()
        rec = await decisions.latest_recommendation(case_id)
        th = await config.resolve("thresholds", at=now())
        flags = collect_review_flags(case, rec, SYSTEM, None)
        gate = compute_gate(case, rec, th, flags, gates_pass=all_hard_gates_pass(case))
        if gate.tier != "auto":
            return await open_approval_task(case, rec, gate)       # agent asks a human
        d = await decisions.create(case_id, kind="final", decided_by="system", ...)
        await audit.append(case_id, "decision.auto_approved", actor=SYSTEM,
                           payload={"outcome": rec.outcome, "amount": str(rec.payable_amount), "gates": gate_results(case), "T_auto": str(th.t_auto), "thresholds_v": th.version})
        return await finalize(task=None, decision=d, approvers=[])
```
`all_hard_gates_pass(case)` is pure code over the verification results (identity score ≥ `identity_min_score`, authenticity score ≥ floor, completeness passed, calculation consistent, no exclusion/waiting-period/high-risk flag); an LLM verdict is never an input.

### 6.3 record_approval
```python
async def record_approval(decision_id, user, verdict, comment):
    task = await tasks.open_for_decision(decision_id)  # 404/409 if none
    async with advisory_lock(task.case_id):
        task = await tasks.reload(task.id)
        if task.status != "open":
            raise TaskClosed()
        d = task.decision
        if user.sub in {a.approver for a in d.valid_approvals}:
            raise AlreadyVoted()  # DB unique too
        if settings.sod and user.sub in {d.created_by, task.case.assignee}:
            raise SodViolation()
        if not has_any_role(user, task.allowed_roles):
            raise RoleNotAllowed()
        if verdict in ("return", "reject") and not comment:
            raise CommentRequired()
        await approvals.add(task.id, d.id, user.sub, verdict, comment, role=primary_role(user))
        if verdict in ("return", "reject"):
            await approvals.invalidate_all(task.id)
            await tasks.close(
                task, "returned" if verdict == "return" else "returned", reason=verdict
            )
            await cases.transition(task.case_id, "ready_for_decision")
            await events.publish("approval.voted", ...)
            return Returned()
        votes = await approvals.valid_approve_votes(task.id)
        seniors = [v for v in votes if "senior_reviewer" in v.roles]
        if len(votes) >= task.required_approvals and len(seniors) >= task.min_senior:
            return await finalize(task, d, approvers=votes)
        return Pending(
            remaining=task.required_approvals - len(votes),
            senior_needed=max(0, task.min_senior - len(seniors)),
        )
```
Note: for a dual task where two non-senior users approve, the task stays open with `senior_needed=1`.

### 6.4 finalize
```python
async def finalize(task, d, approvers):
    final = Decision(
        outcome=d.outcome,
        approved_amount=d.approved_amount,
        deductions=d.deductions,
        reason_codes=d.reason_codes,
        reviewer_ids=[d.created_by, *[a.approver for a in approvers]],
        calc_trace_id=d.calc_result_id,
        policy_version=d.config_versions["policy_rules_v"],
    )
    status = {"approve": "approved", "partial": "partially_approved", "reject": "rejected"}[
        d.outcome
    ]
    async with db.begin():  # one transaction
        await decisions.mark_final(d.id)
        if task:
            await tasks.close(task, "completed")
        await cases.transition(d.case_id, status, approved_amount=d.approved_amount)
        if d.outcome in ("approve", "partial"):
            await utilisation.apply(d.case_id, d.approved_amount)  # may raise SumInsuredExhausted
        await outbox.enqueue(d.case_id, "decisions", final)
        await audit.append(
            d.case_id, "human.approved" if d.outcome != "reject" else "human.rejected", ...
        )
    await events.publish("decision.final", d.case_id, {...})
    if d.outcome != "reject":
        await settlement.initiate_later(d.case_id)  # 06; claim_type decides payee
```
`SumInsuredExhausted` rolls back the transaction, then a compensating transaction creates an escalation (05 `escalate(reason="sum_insured_exhausted")`).

### 6.5 Correction after final
Not allowed in v1. A correction creates a new `decision(kind='final', supersedes=<old>, revision+1)` through dual approval by `senior_reviewer` accounts, and the hospital receives a revised decision with the new `revision` number. The schema reserves the columns; no endpoint ships in v1.

### 6.6 Sequence for a single-approver case
```
Reviewer UI      insurer-api            Redis/SSE        Approver UI       outbox -> hospital
   | submit ------->|                        |                 |                  |
   |                |-- compute_gate         |                 |                  |
   |                |-- task(open), case=awaiting_approval     |                  |
   |                |-- publish approval.requested ----------->|-- toast/queue    |
   |                |                        |                 |-- vote approve ->|
   |                |<-------------------------------------------- POST /approvals|
   |                |-- finalize (tx): decision final, case approved, utilisation, outbox row
   |                |-- publish decision.final ---------------->|                  |
   |                |-- settlement.initiate_later              |   decisions callback delivered async
```

## 7. Config / env vars
| Name | Default | Notes |
|---|---|---|
| `INS_SEGREGATION_OF_DUTIES` | `true` | blocks voting on cases you submitted or are assigned to |
| `INS_AUTO_APPROVE_ENABLED` | `true` | kill switch: `false` forces every decision through a human approver |
| `INS_DECISION_SLA_HOURS` | `24` | SLA used by `approval.overdue` n8n timer (09) |
| `INS_SSE_MAX_CONNECTIONS_PER_USER` | `5` | stream limit |
| `INS_SSE_REPLAY_EVENTS` | `1000` | Last-Event-ID replay window |
| `INS_SSE_HEARTBEAT_SECONDS` | `15` | |
| `INS_EVENTS_STREAM` | `ins:events` | Redis stream name |

Thresholds come from config domain `thresholds` (never env): `t_auto`, `t_four`, `review_flags` (enabled flags and the high-utilisation percentage, default 80), `watchlist_hospitals`. Publishing `thresholds` requires the two-person rule (01-03 section 5).

## 8. Error handling and edge cases
| Situation | Behaviour |
|---|---|
| Thresholds changed while a task is open | Task keeps `threshold_snapshot`; recompute happens only if the recommendation changes (new verification run), then old task `cancelled` and a new one opens |
| Approver is also the reviewer/assignee | Blocked by SoD (`403 sod_violation`); UI shows the reason; admin can disable SoD per environment only |
| Two approvers vote at the same moment | Advisory lock plus unique index; loser gets `409 already_voted` or `task_closed` |
| First approver approves, second rejects | Case returns to reviewer; first approval kept but `valid=false` because the decision may change |
| Reviewer edits amount after approvals started | Not allowed; the task is locked; senior must `withdraw-task` or an approver `return`s |
| Zero payable (fully excluded) | Outcome must be `reject` with reason codes; `approve` with 0 returns `422` |
| Payable greater than remaining sum insured | Calc engine already caps; gate re-checks; mismatch raises `gate.calc_inconsistent` and blocks submit |
| Hospital callback fails | Decision stays final; outbox retries; UI shows "delivery pending"; dead letter after 8 attempts raises `delivery.dead_letter` |
| Utilisation exhausted at finalize | Transaction rolled back; escalation with `sum_insured_exhausted` |
| Reject of a large claim by one approver | Does not finalize; goes back (6.3) |
| Approver role revoked mid-task | Vote attempt checks live Keycloak roles; blocked |
| User disabled / deleted | Past votes keep `approver` sub and role snapshot |
| Rounding | `Decimal` quantised to 0.01 ROUND_HALF_UP everywhere (matches calc engine) |
| Time | all timestamps UTC; UI formats to IST |
| SSE client reconnects | `Last-Event-ID` replays missed events within window; beyond window returns `event: resync` so UI refetches |
| SSE event for case the user cannot see | filtered by audience and case access; never written to their frame |
| Proxy buffering | response headers `Cache-Control: no-cache`, `X-Accel-Buffering: no` |

## 9. Tests
### 9.1 Unit: gate matrix (parametrised)
| ID | claimed | payable | outcome | flags | expected tier |
|---|---|---|---|---|---|
| G1 | 49,999.99 | 49,999.99 | approve | none | auto |
| G2 | 50,000.00 | 50,000.00 | approve | none | auto |
| G3 | 50,000.01 | 50,000.01 | approve | none | single_approver |
| G4 | 10,000 | 10,000 | approve | fraud_warning | single_approver |
| G5 | 4,99,999.99 | 4,00,000 | partial | none | single_approver |
| G6 | 5,00,000.00 | 5,00,000 | approve | none | single_approver |
| G7 | 5,00,000.01 | 5,00,000.01 | approve | none | dual_approver |
| G8 | 6,00,000 | 0 | reject | none | dual_approver |
| G9 | 80,000 | 20,000 | partial | none | single_approver |
| G10 | 20,000 | 20,000 | approve | none, `INS_AUTO_APPROVE_ENABLED=false` | single_approver |
| G11 | 20,000 | 20,000 | approve | all flags clear but identity score below `identity_min_score` (gate fails) | single_approver |
| G12 | 20,000 | 0 | reject | none | single_approver (rejections are never automatic) |

### 9.2 Unit: approval rules
SoD on/off; role not allowed; same user twice; dual with two non-seniors stays open; dual with one senior and one approver finalizes; return invalidates votes; comment required.

### 9.3 Property tests (hypothesis)
- No sequence of votes by one user can finalize a dual task.
- `compute_gate` is monotonic: increasing the gate amount or adding flags never lowers the tier.
- Approved amount never exceeds calc payable unless override recorded.

### 9.4 Integration
- Auto tier end to end (`auto-decide` → final decision with actor=system, `decision.auto_approved` audited, callback payload validates against `claim_contract.models.Decision`, settlement triggered); false-approve guard: any failed gate or flag must never reach the auto tier (property test).
- Single approver, dual approver, return flow, second-approver reject flow.
- Utilisation exhaustion path creates escalation, no partial writes.
- Outbox delivery with a failing hospital stub, then success; sequence ordered.
- Concurrency: five parallel approvals on one task produce exactly one finalize (asserts one outbox row, one utilisation increment).

### 9.5 SSE tests
- Connect as reviewer, approver, senior; each receives only its audience events.
- Replay with `Last-Event-ID`; beyond window gets `resync`.
- Sixth connection rejected; heartbeat arrives; redis down returns 503.
- PII check: fuzz payloads with names and IDs; `redact()` output contains none.

### 9.6 Audit
Every human action recorded with actor and role; `verify_chain(case_id)` passes after the full flow.

## 10. Acceptance criteria
- [ ] Gate behaviour matches section 3.1 and 3.2 for all boundary tests.
- [ ] No path finalizes a dual task without two distinct approvers including one senior.
- [ ] Final decision callback carries all config version stamps.
- [ ] Manual increase above calc result blocked for reviewers; allowed for seniors only with `override_reason` and the `manual_increase` flag.
- [ ] Utilisation never exceeds sum insured (plus bonus) in the concurrency test.
- [ ] `/v1/events/stream` delivers the events in 4.4 with correct audience filtering, replay and heartbeat.
- [ ] 100% branch coverage on `gate.py` and `approval_rules.py`.

## 11. Dependencies
03 (recommendation, findings), 07 (calc result), 09 (flows consume events), 06 (settlement), 10 (UI queues and SSE), 01-04 audit, 01-03 config. Redis from Dev A infra (04-shared-services doc 01).

## 12. Claude Code kickoff prompt
> Read the shared-contract docs and docs/implementation/03-dev-B-insurer/04-api-decision-gate.md. Plan first. Implement tasks 1-18 in order, starting with the pure gate function, flags and their boundary tests (section 9.1) before any endpoint. Use stubbed recommendation fixtures; do not require crews. Implement the SSE endpoint (section 4.4) with the audience filter. Report against section 10 and list any decision you changed from PROPOSED.
