# 03-05 — insurer-api: Query Loop, Triage, Round-3 Escalation (2.2.4)

Owner: Dev B. Status: PROPOSED.
Depends on: 03 (findings become queries), 08 (query drafter and triage agents), 09 (timers), 02 (response endpoint auth), 04 (event stream, gate), contract `Query`, `QueryResponse` (01-02), callbacks (01-01).
Consumed by: 10 (query workspace UI), tpa-sim (scripted hospital replies).

## 1. Goal
Run the multi-round insurer query loop end to end:

1. Turn fixable verification findings into grounded queries (an agent drafts, a human approves).
2. Send them to the hospital through the signed callback channel.
3. Receive responses, triage them, and re-run only the impacted verification steps.
4. On round 3, stop looping and escalate to a senior human.

Principles carried over from the architecture file: *fixable before fatal* (a missing document becomes a query, not a rejection), *human authority* (no query leaves without human approval except whitelisted templates), and *evidence over confidence* (an agent cannot declare a finding resolved; only deterministic re-verification can).

Non-goals: verification logic itself (03), the money decision (04), the crew prompts (08).

## 2. Inputs / Outputs
- Input:
  - `needs_info` outcome with fixable findings from 03
  - hospital `QueryResponse` posts (HMAC authenticated)
  - timers from n8n (reminders, round timeouts)
  - reviewer and senior reviewer actions
- Output:
  - `query`, `query_round` rows
  - callbacks to `/v1/insurer-callbacks/queries` and `/status` (outbox, sequence numbered)
  - case transitions `needs_info ⇄ verifying`, `escalated`
  - audit events, SSE events (04 section 4.4), metrics

### 2.1 Case-level picture
```
verifying --(blockers, all fixable)--> needs_info  [round N opens, queries drafted]
needs_info --(reviewer sends)--> waiting on hospital
waiting --(response)--> triage --(sufficient|partial)--> verifying (impacted steps re-run)
   verifying --(no fixable blockers)--> ready_for_decision
   verifying --(blockers remain, N < 3)--> needs_info  [round N+1 opens]
   verifying --(blockers remain, N = 3)--> escalated
waiting --(due_by passes, no response, N = 3)--> escalated
waiting --(due_by passes, N < 3)--> reminder / reviewer decides to force next round
escalated --(senior resolve)--> ready_for_decision | needs_info (single extension) | rejected path via 04 gate
```

## 3. Data model
Table `core.query` exists (01-insurer-db section 3.2). This doc adds (PROPOSED migration 0010):
```sql
ALTER TABLE core.query
  ADD COLUMN finding_keys TEXT[] NOT NULL DEFAULT '{}',          -- findings this query is meant to close
  ADD COLUMN dedupe_key CHAR(64) NOT NULL,
  ADD COLUMN reminder_count SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN auto_send BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent_draft'
        CHECK (origin IN ('agent_draft','human','scripted','template')),
  ADD COLUMN draft_text TEXT,
  ADD COLUMN draft_citations JSONB NOT NULL DEFAULT '[]',       -- [{type: finding|clause|requirement, ref, snippet}]
  ADD COLUMN draft_source TEXT CHECK (draft_source IN ('llm','template')),
  ADD COLUMN lint_errors JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN sent_at timestamptz,
  ADD COLUMN acked_at timestamptz,                               -- hospital callback delivery confirmed
  ADD COLUMN is_extension BOOLEAN NOT NULL DEFAULT false;        -- senior-requested extra query in round 3
CREATE UNIQUE INDEX ux_query_open_dedupe ON core.query(case_id, dedupe_key) WHERE status IN ('open','draft_ready');

CREATE TABLE core.query_round (
  case_id UUID NOT NULL REFERENCES core.claim_case(id),
  round SMALLINT NOT NULL CHECK (round BETWEEN 1 AND 3),
  opened_at timestamptz NOT NULL,
  due_by timestamptz NOT NULL,
  closed_at timestamptz,
  outcome TEXT CHECK (outcome IN ('resolved','partially_resolved','unanswered','escalated')),
  PRIMARY KEY (case_id, round)
);

CREATE TABLE core.query_response (
  id UUID PRIMARY KEY,
  query_id UUID NOT NULL REFERENCES core.query(id),
  answer_text TEXT NOT NULL,
  attached_doc_ids UUID[] NOT NULL DEFAULT '{}',
  responded_by TEXT NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  idempotency_key UUID NOT NULL,
  triage JSONB,                                    -- TriageResult, nullable until triaged
  triage_source TEXT CHECK (triage_source IN ('llm','rules','reviewer_override')),
  amends UUID REFERENCES core.query_response(id)   -- one amendment allowed while status='answered'
);
CREATE UNIQUE INDEX ux_qresp_idem ON core.query_response(query_id, idempotency_key);
```

### 3.1 Query status machine
Contract `QueryStatus` is `open, draft_ready, answered, closed, escalated`. "Sent" is `open` with `sent_at` set.
```
(created) --draft saved--> draft_ready --human approves--> open(sent_at) --hospital responds--> answered --triage+reverify--> closed
                              \--lint fail--> stays draft_ready (lint_errors shown)
open(sent_at) --round 3 timeout / unresolved--> escalated
open --reviewer close--> closed (reason)
```

### 3.2 SLA and policy defaults (config domain `query_policy`, PROPOSED)
| Key | Default |
|---|---|
| `max_rounds` | 3 |
| `round_sla_hours` | [72, 48, 24] |
| `reminder_offsets_hours` | [24, 6] before due |
| `auto_send_categories` | ["missing_document"] (round 1 only, template text only) |
| `max_queries_per_round` | 1 (one consolidated message) |
| `allowed_extension` | 1 (senior can add one extra round-3 query) |
| `escalation_role` | `senior_reviewer` |
| `skip_weekends` | false |

### 3.3 Category mapping
| Finding family (03) | `QueryCategory` | Requested docs (default) |
|---|---|---|
| `doc.missing.*` | `missing_document` | per `doc_requirements` version |
| `doc.illegible.*`, `doc.quality.*` | `illegible_document` | re-upload of same doc types |
| `identity.mismatch.*` | `identity_mismatch` | `id_proof`, `policy_card` |
| `medical.inconsistent.*` | `medical_clarification` | `investigation_report` or note |
| `bill.discrepancy.*` | `billing_discrepancy` | `itemised_bill`, `final_bill` |
| `coverage.exclusion.*` (fixable) | `policy_exclusion` | clarification text |
| anything else fixable | `other` | none |

## 4. API / endpoints
Auth: Keycloak `insurer` realm for reviewer endpoints; HMAC (01-01 section 3) for hospital-facing; service account for `/internal/*`.

### 4.1 Reviewer endpoints
| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/v1/cases/{id}/queries` | reviewer+ | all rounds, responses and triage, lint and citations |
| POST | `/v1/cases/{id}/queries/draft` | reviewer | triggers crew draft from open fixable findings; returns drafts (202 if async, polls via SSE `query.draft_ready`) |
| PATCH | `/v1/queries/{qid}` | reviewer | edit `text`, `requested_doc_types`, `due_by` (within policy); requires `If-Match` |
| POST | `/v1/queries/{qid}/send` | reviewer | approve and send; audit `query.sent` |
| POST | `/v1/queries/{qid}/close` | reviewer | close without waiting; `{reason}` required |
| POST | `/v1/queries/{qid}/triage/override` | reviewer | override verdict `{verdict, note}` |
| POST | `/v1/cases/{id}/queries` | reviewer | human-authored query (origin `human`) |
| GET | `/v1/escalations` | senior_reviewer | escalation queue with pack summary |
| GET | `/v1/cases/{id}/escalation` | senior_reviewer | full escalation pack |
| POST | `/v1/cases/{id}/escalation/resolve` | senior_reviewer | `{action: request_more | decide_now | reject_for_noncompliance, note, query?}` |

### 4.2 Hospital-facing (HMAC, doc 02)
- `GET /v1/hospital-api/claims/{claim_ref}/queries`
- `POST /v1/hospital-api/queries/{query_id}/responses` body `QueryResponse {query_id, answer_text, attached_doc_ids[], responded_by}`. Attached docs must already exist through the supplement endpoint (02 section 4.3) in this or an earlier call. Returns `202`.

### 4.3 Internal (n8n)
- `POST /internal/queries/{qid}/triage-result` (n8n posts crew triage output; API cross-checks, see 6.4)
- `POST /internal/queries/{qid}/reminder`
- `POST /internal/cases/{id}/round-timeout` (timer fired; `{round}`)

### 4.4 Insurer-crew endpoints this doc calls (defined in 08; **canonical paths**)
| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/v1/query/draft` | `QueryDraftInput {case_summary, findings[], requirements[], round, policy_excerpts[]}` | `QueryDraftOutput {text, requested_doc_types[], citations[], category}` |
| POST | `/v1/query/triage` | `TriageInput {query, response, new_doc_summaries[]}` | `TriageOutput {verdict, resolved_finding_keys[], remaining_finding_keys[], notes}` |

> Reconciliation note: earlier drafts used `/draft-query` and `/triage-response`. The canonical paths are `/v1/query/draft` and `/v1/query/triage` (08 section on crew API). Client constants live in `clients/crew.py` as `PATH_QUERY_DRAFT` and `PATH_QUERY_TRIAGE`; no other file hard-codes a path.

### 4.5 Callback to the hospital: `/v1/insurer-callbacks/queries`
```json
{"query_id":"b7e8...","claim_ref":"HC-2026-000045","round":1,"category":"missing_document",
 "text":"Please provide the signed final bill and the implant sticker for the knee prosthesis (line 14).",
 "requested_doc_types":["final_bill","implant_sticker"],
 "due_by":"2026-10-09T10:00:00Z","sequence":4}
```
Response example from hospital:
```json
POST /v1/hospital-api/queries/b7e8.../responses
{"query_id":"b7e8...","answer_text":"Signed final bill and implant sticker attached.",
 "attached_doc_ids":["d11a...","d11b..."],"responded_by":"desk.rao"}
```
`202 {"status":"received","query_status":"answered"}`; errors: `404 unknown_query`, `409 query_closed`, `422 missing_attachments`, `424 doc_unavailable`.

### 4.6 Events published (to `/v1/events/stream`, see 04 section 4.4)
`query.draft_ready`, `query.sent`, `query.answered`, `query.overdue`, `escalation.raised`, plus `case.status_changed`. Payloads carry ids and round, never answer text.

## 5. Build tasks
Paths relative to `insurer/api/app/`.
1. Migration `alembic/versions/0010_query_loop.py` (section 3); upgrade/downgrade test.
2. `services/query_builder.py`: groups fixable findings, computes `dedupe_key`, maps categories (3.3), enforces one consolidated query per round (6.1).
3. `clients/crew.py`: `draft_queries(context)` → `POST {INS_CREW_URL}/v1/query/draft`; `triage(query, response, docs)` → `POST /v1/query/triage`; timeout `INS_CREW_TIMEOUT_SECONDS`; retries (2, exponential); structured error mapping to `CrewUnavailable`.
4. `services/query_lint.py`: deterministic lint (6.2); returns list of `LintError(code, detail)`.
5. `services/query_templates.py` + `templates/queries/{category}.j2`: deterministic fallback text per category with Jinja2 strict-undefined.
6. Draft policy in `services/query_draft.py`: always saves `draft_ready`; auto-send only when category is whitelisted, round is 1, `draft_source='template'` and lint passes; otherwise human approval.
7. `services/query_send.py`: validates round/SLA, sets `sent_at`, creates round row if needed, enqueues outbox `queries` callback, sets case `needs_info`, audit `query.sent`, publishes SSE.
8. `services/query_response.py`: intake (6.3): validate ownership and state, idempotency, store response, link docs, status `answered`, call n8n webhook for triage.
9. `services/triage.py`: applies TriageOutput; cross-check rules (6.4); manual override path.
10. `services/rounds.py`: open/close rounds, due-date maths (calendar hours, optional skip weekends), advance, max-rounds enforcement.
11. `services/escalation.py`: raise escalation, assemble pack (6.5), resolve actions (6.6).
12. Reminder and timeout handlers (`api/internal_queries.py`) invoked by n8n (09): reminders at `due_by - 24h` and `-6h`, timeout at `due_by`.
13. Hospital callbacks: `status` callback after each transition using the shared outbox with a per-claim sequence.
14. `api/queries.py` and `api/escalations.py` routers for 4.1-4.3 with RBAC and ETag checks.
15. Delivery confirmation: outbox success sets `query.acked_at`; due dates are computed from `acked_at` when present else `sent_at` (PROPOSED).
16. tpa-sim hook: `origin='scripted'` queries from the scripted generator for tests (06-tpa-sim doc).
17. Metrics endpoint `/metrics`: rounds per claim, time to response, resolution rate by category, lint failure rate, template fallback rate.
18. Tests (section 9) and demo seed `seeds/query_demo.py`.

## 6. Key logic / pseudocode

### 6.1 Building queries from findings
```python
def build_queries(case, findings, policy, rnd):
    groups = defaultdict(list)
    for f in findings:
        if (
            f.severity == "blocker"
            and f.fixable
            and not f.overridden
            and not already_resolved(case, f)
        ):
            groups[f.suggested_query_category].append(f)
    drafts = []
    for cat, fs in groups.items():
        keys = sorted({f.key for f in fs})
        docs = sorted({d for f in fs for d in f.suggested_doc_types})
        dedupe = sha256("|".join(keys + docs) + f"|r{rnd}")
        if exists_open(case.id, dedupe):
            continue
        drafts.append(
            QueryDraft(
                category=cat,
                finding_keys=keys,
                requested_doc_types=docs,
                dedupe_key=dedupe,
                round=rnd,
                due_by=now() + hours(policy.round_sla_hours[rnd - 1]),
            )
        )
    return consolidate(drafts, max_per_round=policy.max_queries_per_round)
```
`consolidate` merges drafts into one message whose body has one section per category (default `max_queries_per_round=1`), keeping `finding_keys` and `requested_doc_types` as unions. Reason: fewer round trips for hospital desks.

`already_resolved` checks the document set first: if the hospital already supplied a document that satisfies the finding, the finding is closed (re-verify) and no query is drafted.

### 6.2 Draft lint (before any send)
Deterministic, all must pass:
| Code | Rule |
|---|---|
| `LINT_PII` | no ID numbers, phone numbers, emails (regex plus Presidio) |
| `LINT_SCOPE` | only mentions docs/fields present in `finding_keys` or `requested_doc_types` |
| `LINT_CITATION` | every policy clause cited exists in `draft_citations` and resolves in the KB (`rag-service` lookup by clause id) |
| `LINT_PROMISE` | none of: "will be approved", "guaranteed", "assured payment", "we will pay" |
| `LINT_LENGTH` | ≤ `INS_QUERY_TEXT_MAX` (1200 chars) |
| `LINT_TONE` | checklist: no blame words, ends with the due date and contact line (regex checks) |
| `LINT_DOCS` | every `requested_doc_types` value is a valid `DocType` and allowed by `doc_requirements` |
Failure leaves the query `draft_ready` with `lint_errors`; the reviewer fixes text or regenerates (max 3 regenerations, then template fallback).

### 6.3 Response intake and round state machine
```python
async def on_response(query_id, resp, idem_key):
    async with advisory_lock(case_id_of(query_id)):
        q = await queries.get_for_update(query_id)
        if q.status in ("closed", "escalated") and not allow_late(q):
            raise QueryClosed()
        if exists_idem(q.id, idem_key):
            return replay()
        if q.status == "answered" and not amendment_allowed(q):
            raise Conflict()
        attach_docs(resp.attached_doc_ids)  # verify sha256, size, clamav status
        store(resp)
        q.status = "answered"
        await events.publish("query.answered", ...)
    triage = await triage_service.run(q, resp)  # crew or rules fallback
    run = await verification.rerun(case, trigger="query_response", steps=impacted_steps(q, triage))
    unresolved = [f for f in run.blockers if f.fixable]
    if not unresolved:
        await rounds.close(case, q.round, "resolved")
        await queries.close(q)
        return await cases.transition(case.id, "ready_for_decision")  # via verifying
    if q.round >= policy.max_rounds:  # round 3 reached
        await rounds.close(case, q.round, "escalated")
        return await escalation.raise_(case, unresolved, reason="unresolved_after_round_3")
    await rounds.close(case, q.round, "partially_resolved")
    await open_next_round(case, unresolved)  # drafts round+1 for reviewer approval


async def on_timeout(case, round_no):
    await rounds.close(case, round_no, "unanswered")
    if round_no >= policy.max_rounds:
        return await escalation.raise_(case, reason="no_response_round_3")
    await notify_reviewer(
        case, "query.overdue"
    )  # PROPOSED: unanswered round does NOT auto-open next round
```
Rule: round 2 opens only after a response or after a reviewer forces it; unanswered round 1 or 2 raises reminders and an overdue alert, and escalates only after the round-3 deadline passes (or a senior forces it).

### 6.4 Triage cross-check
`TriageOutput.verdict` is advisory. Server rules after crew returns:
```python
def apply_triage(q, resp, t):
    # 1. an agent cannot resolve a finding the code still sees as open
    t.resolved_finding_keys = [k for k in t.resolved_finding_keys if k in q.finding_keys]
    # 2. deterministic completeness check against requested_doc_types
    missing = set(q.requested_doc_types) - classify(resp.attached_doc_ids)
    if missing and t.verdict == "sufficient":
        t.verdict = "partial"
        t.remaining_finding_keys += keys_for(missing)
    # 3. empty answer with no docs
    if not resp.answer_text.strip() and not resp.attached_doc_ids:
        t.verdict = "insufficient"
    return t
```
Verdict handling:
- `sufficient` → re-verify impacted steps.
- `partial` → re-verify; remaining items go into the next round draft.
- `insufficient` / `off_topic` → no re-verification; reviewer decides next round or escalate (round 3: escalate).
Closure always depends on re-verification, never on the verdict alone. Reviewer can override a verdict with a note (`triage_source='reviewer_override'`, audited).

LLM down fallback: `triage_source='rules'`: verdict `sufficient` only if all requested doc types were classified in attachments, else `partial`/`insufficient`.

### 6.5 Grounding and the escalation pack
Draft grounding stored in `draft_citations`: (a) finding codes with evidence doc pages, (b) policy clause ids from RAG, (c) the exact documents required per `doc_requirements` version. The UI shows them beside the draft.

Escalation pack (JSON assembled by `escalation.build_pack`):
```json
{"case_id":"...","reason":"unresolved_after_round_3",
 "timeline":[{"ts":"...","event":"query.sent","round":1}],
 "rounds":[{"round":1,"outcome":"partially_resolved","query_ids":["..."]}],
 "unresolved_findings":[{"key":"bill.discrepancy.line14","evidence":"p3","attempts":3}],
 "responses":[{"query_id":"...","triage":"partial"}],
 "calc_preview":{"payable_if_decided_now":"61250.00"},
 "suggested_actions":["decide_now","reject_for_noncompliance"],
 "risk_flags":["contradictory_responses"]}
```
Escalation reason codes: `unresolved_after_round_3`, `no_response_round_3`, `contradictory_responses`, `fraud_indicator`, `sum_insured_exhausted`.

### 6.6 Escalation resolution actions
| Action | Effect |
|---|---|
| `request_more` | senior writes one extra query; `is_extension=true`; round stays 3; maximum `allowed_extension` (1); due per round-3 SLA |
| `decide_now` | case to `ready_for_decision` with a banner "decided with unresolved findings"; gate forced to at least `single_approver`; flag `escalated_case` |
| `reject_for_noncompliance` | reviewer-style reject decision with reason `docs_not_provided`; goes through gate (dual approval if the claim is above `T_four`) |

## 7. Config / env vars
Domain `query_policy` (3.2). Env: `INS_CREW_URL`, `INS_CREW_TIMEOUT_SECONDS=90`, `INS_QUERY_TEXT_MAX=1200`, `INS_QUERY_REGEN_MAX=3`, `INS_N8N_WEBHOOK_TRIAGE`. RAG is reached through the crew only (`INS_RAG_URL` used for lint clause lookups).

## 8. Error handling and edge cases
| Situation | Behaviour |
|---|---|
| Response for a closed/escalated query | `409 query_closed`; hospital desk shows message. If escalated and not yet resolved, accept it as late response (stored, senior notified) |
| Response arrives after timeout but before escalation resolved | Accepted; case stays `escalated`; pack updated; senior notified |
| Response lacks requested documents | Triage `partial`; remaining list shown; next round draft |
| Duplicate response, same idempotency key | Replay stored result |
| Different content for same query | `409` unless status `answered` and no re-verification started: then one amendment allowed (`amends` link) |
| Attached doc hash mismatch or virus | Response accepted as text but verdict `partial`; finding `doc.integrity.*`; audit |
| Draft contains PII | Lint blocks; fallback to template |
| LLM / crew down or timeout | Deterministic template from `templates/queries/{category}.j2`; `draft_source='template'`; case flag `degraded_mode` |
| Stale finding (hospital already supplied) | Builder checks documents first; finding closed by re-verify, no query |
| Two reviewers edit the same draft | ETag mismatch `412`; UI shows diff |
| Escalation while re-verification is running | Wait for the run (advisory lock) then escalate |
| Hospital unreachable | Outbox retries; due dates run from `acked_at`; if never acked after 8 attempts, dead letter alert and due date from `sent_at` |
| Weekends | Optional `skip_weekends` shifts `due_by` forward (default off) |
| Withdrawn claim during loop | Open queries closed with reason `claim_withdrawn`; no further reminders |
| Reviewer closes a query manually | Reason required; findings remain open, so the next re-verify can re-raise (dedupe prevents duplicates inside the same round) |
| Clock skew between insurer and n8n | n8n calls with the intended `round` number; handler ignores a timeout for a round that is already closed |
| Round counter tampering | Round derived from `query_round` rows only; not from client input |

## 9. Tests
### 9.1 Unit
- Builder grouping, dedupe, consolidation, already-resolved shortcut.
- Lint: one test per code in 6.2 (positive and negative) plus a PII fuzz corpus.
- Due-date maths: SLA arrays, weekends on/off, delivery-ack shift.
- Triage cross-check: agent claims resolved but docs missing → downgraded.

### 9.2 State machine tests (table driven)
| ID | Scenario | Expected final state |
|---|---|---|
| Q1 | resolved in round 1 | `ready_for_decision`, 1 round |
| Q2 | partial in round 1, resolved in round 2 | `ready_for_decision`, 2 rounds |
| Q3 | partial rounds 1-2, resolved round 3 | `ready_for_decision`, 3 rounds |
| Q4 | unresolved after round 3 | `escalated` (reason unresolved_after_round_3), never a 4th round |
| Q5 | never answered through round 3 due | `escalated` (no_response_round_3) |
| Q6 | contradictory responses | `escalated` (contradictory_responses) |
| Q7 | off-topic answer | reviewer decides, no re-verify |
| Q8 | late response while escalated | stored, case stays escalated |
| Q9 | senior `request_more` | one extension only; second attempt rejected |
| Q10 | LLM down | template drafts, still sendable |
Property test: the number of rounds never exceeds 3 (plus at most one extension).

### 9.3 Integration with tpa-sim (scripted hospital)
Profiles: `answers_fully`, `answers_partially`, `off_topic`, `never_answers`, `amends_answer`, `duplicate_post`, `late_after_escalation`. Assert case status, callbacks received by the sim, sequence order.

### 9.4 Other
- Escalation pack snapshot test.
- RBAC: reviewer cannot resolve escalation; approver cannot send queries (PROPOSED: reviewers and seniors only).
- Crew contract tests with recorded fixtures (VCR-style) against `/v1/query/draft` and `/v1/query/triage`; test that no code references the retired `/draft-query` or `/triage-response` strings (grep test).
- Chaos: crew timeout → template fallback; Redis down → events skipped, flow continues.
- Audit: every query, edit, send, response, triage, override and escalation is in the chain and `verify_chain` passes.

## 10. Acceptance criteria
- [ ] Scripted scenarios Q1-Q10 end in the expected states.
- [ ] No query is sent without human approval except whitelisted template auto-sends.
- [ ] Triage cannot close a finding without re-verification passing.
- [ ] Round 3 unresolved or unanswered always escalates; no round 4.
- [ ] Crew paths are `/v1/query/draft` and `/v1/query/triage` everywhere (grep test passes).
- [ ] Every query, edit, send, response, triage and override is in the audit chain.
- [ ] SSE events from 4.6 reach the UI for the correct audiences.

## 11. Dependencies
03 (findings, re-verification), 04 (events, gate), 08 (crew endpoints), 09 (timers), rag-service (via crew, lint lookups), contract callbacks (01-01), tpa-sim (06-tpa-sim), 02 (supplement documents endpoint).

## 12. Claude Code kickoff prompt
> Read the shared-contract docs and docs/implementation/03-dev-B-insurer/05-api-query-loop-escalation.md. Plan first. Implement tasks 1-18, starting with the pure builder, lint and round state machine with the table-driven tests in 9.2, then the API. Use recorded crew fixtures and tpa-sim profiles. Use the canonical crew paths `/v1/query/draft` and `/v1/query/triage`. Report against section 10.
