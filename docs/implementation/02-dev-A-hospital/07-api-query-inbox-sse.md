# 02-07 — hospital-api: Query Inbox, Query Responses and SSE Status Feed

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/{routers/queries.py,services/queries.py,sse/}`.

## 1. Goal
Handle the multi-round insurer query loop on the hospital side (up to 3 rounds): receive queries, triage, obtain a grounded draft reply from the Query Responder agent, let a human edit/approve, attach supplementary documents, send the response, and stream live case updates to the UI over SSE.

Design stance:
- **Never block on an LLM.** Triage has a deterministic fallback; drafting is optional assistance; a human can always write the reply.
- **Grounded or flagged.** A deterministic guard checks every citation and number in an agent draft before the Officer sees it as "ready".
- **Human authority.** No response is sent without an approved version; round 3 needs two people.
- **One event bus.** Every state change publishes one event; SSE is just a filtered, authorised view of that bus.

### 1.1 Round lifecycle
```
insurer raises Q(round n) ─callback─▶ insurer_query[open] ─triage─▶ [triaged]
        ▲                                                        │ draft (crew, optional)
        │ insurer closes / next round                            ▼
 [closed|escalated] ◀─ send ◀─ approve ◀─ edit ◀──── response v1 (agent) / v1 (human)
                         │ outbox → insurer
                         ▼
                   [answered]  (case → acknowledged when no open queries remain)
```

## 2. Inputs / Outputs
- In: `Query` callbacks (contract), officer/desk actions, agent drafts.
- Out: `insurer_query`, `query_response` rows; outbox messages `POST /v1/hospital-api/queries/{id}/responses`; SSE events; reminders for SLA.

## 3. Data model
Tables `insurer_query`, `query_response`, `doc_request` (doc 01). Triage JSON: `{"class":"missing_document|clarification|billing|medical|policy|other","urgency":"normal|urgent","owner_role":"desk|officer","needs_docs":["lab_report"],"auto_draftable":true}`.

### 3.1 DDL recap
```sql
CREATE TABLE insurer_query (
  id                uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id           uuid NOT NULL REFERENCES claim_case(id),
  insurer_query_id  text NOT NULL,
  round             int  NOT NULL CHECK (round BETWEEN 1 AND 3),
  category          text NOT NULL,                  -- QueryCategory (contract 01-02)
  text              text NOT NULL,
  requested_doc_types text[] NOT NULL DEFAULT '{}',
  due_by            timestamptz NOT NULL,
  status            text NOT NULL CHECK (status IN ('open','draft_ready','answered','closed','escalated')),
  triage            jsonb,
  triage_source     text,                           -- 'crew' | 'rules'
  escalation_risk   boolean NOT NULL DEFAULT false,
  assigned_to       uuid REFERENCES app_user(id),
  received_at       timestamptz NOT NULL DEFAULT now(),
  answered_at       timestamptz,
  UNIQUE (case_id, insurer_query_id)
);
CREATE INDEX ix_query_inbox ON insurer_query (status, due_by);

CREATE TABLE query_response (
  id            uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  query_id      uuid NOT NULL REFERENCES insurer_query(id),
  version       int  NOT NULL,
  source        text NOT NULL CHECK (source IN ('agent','human','human_edit')),
  status        text NOT NULL CHECK (status IN ('draft','needs_attention','approved','sent','superseded')),
  draft_text    text NOT NULL,
  citations     jsonb NOT NULL DEFAULT '[]',
  attached_doc_ids uuid[] NOT NULL DEFAULT '{}',
  grounding     jsonb,                              -- guard result: {ok, unsupported_claims[], checked_at}
  override_note text,                               -- required when approving a needs_attention draft unchanged
  model_info    jsonb,
  approved_by   uuid[] NOT NULL DEFAULT '{}',       -- 1 or 2 officers
  approved_at   timestamptz,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (query_id, version)
);

CREATE TABLE reminder (
  id uuid PRIMARY KEY DEFAULT gen_uuid_v7(), case_id uuid NOT NULL, subject_type text NOT NULL, subject_id uuid NOT NULL,
  fire_at timestamptz NOT NULL, kind text NOT NULL, fired_at timestamptz, cancelled_at timestamptz);
CREATE INDEX ix_reminder_due ON reminder (fire_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
```

## 4. API / endpoints
```
GET  /v1/queries?status=&case_id=&owner=        inbox (scoped)
GET  /v1/queries/{id}                            query + triage + responses + citations
POST /v1/queries/{id}/triage                     override {class, owner_role}
POST /v1/queries/{id}/draft                      (re)generate draft via crew -> 202
PUT  /v1/queries/{id}/responses/{ver}            edit draft text / attachments
POST /v1/queries/{id}/responses/{ver}/approve    officer
POST /v1/queries/{id}/send                       officer (needs approved version) -> outbox
POST /v1/queries/{id}/documents                  upload supplementary docs (same ingest as doc 03, flagged supplementary)
Internal: POST /v1/internal/queries/{id}/triage-result, /draft-result (svc-crew)
Callback (HMAC): POST /v1/insurer-callbacks/queries
SSE: GET /v1/stream?case_id=&scope=inbox      text/event-stream (JWT via query param ticket)
POST /v1/stream/ticket                          -> {ticket, expires_in:30}  (one-time, avoids token in URL)
```
Draft result example:
```json
{"draft_text":"Dear Reviewer, the discharge summary (attached, page 2) records ... ","citations":[{"doc_id":"...","page":2,"quote":"Patient presented with chest pain on 28-Sep"}],
 "attach_suggestions":["<doc_id>"],"unsupported_claims":[],"model_info":{"alias":"draft-main","prompt_version":"qr-v3"}}
```

### 4.1 Examples
**Insurer callback in** (`POST /v1/insurer-callbacks/queries`, signed per 01-01 §3)
```json
{"claim_ref":"HC-2026-000498","sequence":4,"query_id":"Q-2026-001207","round":1,"category":"medical_clarification",
 "text":"Please clarify why ICU stay of 3 days was required for an uncomplicated stent procedure; attach ICU chart.",
 "requested_doc_types":["investigation_report"],"due_by":"2026-10-09T10:00:00Z"}
```
Responses: `202 {"query_id":"…","status":"open"}` (new), `200` + `Idempotent-Replay: true` (dup), `404 unknown_claim`, `422 max_rounds_exceeded` (round 4), `422 validation_error`.

**Inbox**
```http
GET /v1/queries?status=open,draft_ready&owner=me&sort=due_by&limit=25
```
```json
{"counts":{"open":6,"draft_ready":3,"awaiting_approval":2,"overdue":1},
 "items":[{"id":"q1…","case_ref":"HC-2026-000498","patient_initials":"R.S.","round":1,"category":"medical_clarification",
   "triage":{"class":"medical","urgency":"normal","owner_role":"officer","auto_draftable":true},
   "status":"draft_ready","due_by":"2026-10-09T10:00:00Z","overdue":false}],
 "next":"eyJkdWUiOiIyMDI2LTEwLTA5VDEwOjAwOjAwWiIsImlkIjoicTEifQ"}
```
Pagination: keyset on `(due_by, id)`; `next` is an opaque base64 cursor. `patient_initials` rather than names in list view (privacy by construction).

**Draft detail**
```json
{"id":"q1…","text":"Please clarify why ICU stay …","round":1,"status":"draft_ready",
 "responses":[{"version":1,"source":"agent","status":"needs_attention",
   "draft_text":"ICU admission was required because post-procedure troponin remained elevated (see ICU chart page 2)…",
   "citations":[{"doc_id":"d9…","page":2,"quote":"troponin 4.8 ng/mL at 6 h post procedure","verified":true}],
   "grounding":{"ok":false,"unsupported_claims":[{"type":"number","text":"3 days","reason":"not found in cited sources"}]},
   "attached_doc_ids":["d9…"]}],
 "history":[{"round":0,"note":"claim submitted"}]}
```
**Edit / approve / send**
```http
PUT  /v1/queries/q1…/responses/1   {"draft_text":"…","attached_doc_ids":["d9…","e3…"]}   → 200 {"version":2,"source":"human_edit","grounding":{"ok":true}}
POST /v1/queries/q1…/responses/2/approve  {"override_note":null}                            → 200 {"approved_by":["u-off-12"],"needs_second_approver":false}
POST /v1/queries/q1…/send                                                                    → 202 {"outbox_ids":["…","…"],"status":"answered"}
```
Errors: `409 response_not_approved`, `409 needs_second_approver` (round 3), `422 ungrounded_requires_override_note`, `409 same_user_cannot_second_approve`, `412` stale version.

**Stream ticket and SSE**
```http
POST /v1/stream/ticket  {"scope":"inbox"}  → {"ticket":"tk_8fd…","expires_in":30}
GET /v1/stream?ticket=tk_8fd…&scope=inbox
Accept: text/event-stream     Last-Event-ID: 1728209661234-0

HTTP/1.1 200 OK  Content-Type: text/event-stream  Cache-Control: no-cache  X-Accel-Buffering: no

: heartbeat

id: 1728209661240-0
event: query.new
data: {"type":"query.new","case_id":"6f1c…","ts":"2026-10-06T10:16:01Z","data":{"query_id":"q1…","round":1}}
```

## 5. Build tasks
1. Callback receiver `/v1/insurer-callbacks/queries`: HMAC middleware, dedup via `inbound_callback`, validate `Query` (round 1-3, due_by), upsert `insurer_query` by `insurer_query_id`, case → `under_query`, audit `query.received`, publish SSE `query.new`, enqueue triage in n8n (doc 08 flow `query-intake`).
2. Triage service: n8n calls crew triage (doc 09) → internal endpoint stores `triage`; deterministic fallback `triage_rules(category)` (mapping from `QueryCategory`) used if crew fails or confidence missing — never block on LLM.
3. Draft generation endpoint: collects context package (case facts, parsed docs masked, claim draft, prior rounds' Q&A, KB passages if policy question) → crew `/v1/jobs/query-draft`; stores result as `query_response` v(n+1), `source='agent'`, `status='draft'`.
4. Grounding guard (deterministic, before showing draft): every citation `doc_id` belongs to case; quote string actually appears in the masked text of that doc page (substring/ fuzzy ≥ 0.9); numbers in draft (amounts, dates) must appear in cited sources or case data; otherwise set `unsupported_claims` and mark draft `needs_attention` (UI highlights).
5. Edit endpoint (human edits create new version `human_edit`); diff stored; audit `query.edited`.
6. Approve: Officer only; for round 3 or categories `policy_exclusion`, `identity_mismatch`: second-person confirmation required? PROPOSED: round 3 requires Officer + another Officer ("escalation-grade") — config `query_policy.hospital_round3_two_person=true`.
7. Attachments: select existing case docs and/or upload new ones; new docs go through ingest (doc 03) with `supplementary=true`; they are included as `DocumentRef` presigned URLs in the response payload (contract `QueryResponse.attached_doc_ids` plus documents supplement call).
8. Send: single DB transaction → outbox rows: (a) `POST /claims/{ref}/documents` if new docs, (b) `POST /queries/{id}/responses`; status `answered`; case back to `acknowledged` when all open queries answered.
9. Round tracking: UI shows "Round x of 3"; if insurer raises round 3, flag `escalation_risk` and notify Officer; after answer wait.
10. SLA: `due_by` comes from the query. No escalation reminders at 50%/80% elapsed (user decision); the only SLA signal is overdue at 100% (`now > due_by`): the row is marked red in the inbox and SSE `query.overdue` is emitted once. Escalation happens only on round 3 (see 05 on the insurer side).
11. SSE hub `sse/hub.py`: Redis pub/sub channel `hosp.events`; each API instance subscribes, fans out to connected clients filtered by scope; event schema `{id, type, case_id, ts, data}`; heartbeat comment every 15 s; `Last-Event-ID` resume from Redis stream `hosp.events.stream` (maxlen 10k). Authorisation per event using same `case_scope` rule.
12. Event types: `case.status_changed`, `case.route_changed`, `doc.uploaded|scanned|parsed|classified`, `completeness.updated`, `claim.draft_ready`, `claim.validation_failed`, `submission.sent|failed|dead`, `query.new|draft_ready|overdue|answered`, `decision.received`, `settlement.received`.
13. Publisher helper `events.publish(type, case_id, data)` called by services after commit (use `after_commit` hook so rollback never emits).
14. Ticket endpoint: short-lived one-time ticket in Redis (30 s) so EventSource (no headers) can authenticate; scope bound to user.
15. Inbox query with filters, counts per tab (open, draft ready, awaiting approval, overdue), keyset pagination.
16. Closing a query: insurer closes via status callback; mark `closed`; stale drafts archived.

### 5.1 Step detail: triage fallback mapping (task 2)
| `QueryCategory` | class | owner_role | urgency | `auto_draftable` | `needs_docs` |
|---|---|---|---|---|---|
| `missing_document` | missing_document | desk | normal | false | from `requested_doc_types` |
| `illegible_document` | missing_document | desk | normal | false | the named doc type |
| `identity_mismatch` | clarification | officer | urgent | false | `id_proof` |
| `medical_clarification` | medical | officer | normal | true | `investigation_report`, `lab_report` if requested |
| `billing_discrepancy` | billing | officer | normal | true | `itemised_bill` |
| `policy_exclusion` | policy | officer | urgent | true (needs KB) | — |
| `other` | other | officer | normal | false | — |
Urgency overrides: `due_by − now < 24 h` → `urgent`; round 3 → `urgent`, `escalation_risk=true`. `triage_source='rules'` when fallback used; crew result replaces it only if its schema validates and `class` ∈ vocabulary.

### 5.2 Step detail: grounding guard (task 4)
```python
def check_grounding(draft: DraftResult, case_ctx: GroundingCtx) -> Grounding:
    unsupported = []
    # 1. citations
    for c in draft.citations:
        doc = case_ctx.docs.get(c.doc_id)
        if doc is None:
            unsupported.append(("citation", c.doc_id, "doc_not_in_case"))
            continue
        page_text = case_ctx.page_text(doc.id, c.page)  # masked text stored by doc-pipeline
        if page_text is None:
            unsupported.append(("citation", c.doc_id, "page_not_found"))
            continue
        if not (
            normalise(c.quote) in normalise(page_text)
            or fuzz.partial_ratio(c.quote, page_text) >= 90
        ):
            unsupported.append(("citation", c.quote[:40], "quote_not_found"))
    # 2. numbers and dates in the draft body
    cited_text = (
        " ".join(case_ctx.page_text(c.doc_id, c.page) or "" for c in draft.citations)
        + case_ctx.facts_text
    )
    for tok in extract_numbers_and_dates(
        draft.draft_text
    ):  # INR amounts, dd-mm-yyyy/yyyy-mm-dd, "3 days", ICD codes
        if canonical(tok) not in canonical_set(cited_text):
            unsupported.append(("number", tok, "not_found_in_cited_sources"))
    # 3. forbidden commitments
    for pat in FORBIDDEN_PATTERNS:  # "we guarantee", "approved", "will pay", legal threats
        if pat.search(draft.draft_text):
            unsupported.append(("policy", pat.pattern, "forbidden_commitment"))
    return Grounding(ok=not unsupported, unsupported_claims=unsupported)
```
Normalisation: lowercase, collapse whitespace, strip punctuation, unify digit separators (`1,80,000` ≡ `180000` ≡ `1.8 lakh`), Indian date formats. Result stored on `query_response.grounding`; `status='needs_attention'` when `ok=false`.

### 5.3 Step detail: approval rules (task 6)
| Condition | Required approvals |
|---|---|
| Round 1–2, grounded, category not policy/identity | 1 Officer |
| Round 1–2, `needs_attention` unchanged | 1 Officer + `override_note` ≥ 20 chars |
| Round 3 (any) or category in {`policy_exclusion`,`identity_mismatch`} | 2 distinct Officers (config `hospital_round3_two_person`) |
| Response with new documents | same as above; documents must be `clean` and classified |
Approvals attach to a specific `version`; any edit creates a new version and clears approvals. The sender of record is the second approver.

### 5.4 Step detail: send transaction (task 8)
```python
async def send(query_id, user):
    async with db.begin():
        q = await queries.get_for_update(query_id)
        resp = await responses.latest_approved(q.id)
        if not resp:
            raise Problem(409, "response_not_approved")
        if q.status == "answered":
            raise Problem(409, "already_answered")
        new_docs = [d for d in resp.attached_doc_ids if await docs.is_supplementary_unsent(d)]
        seq = await outbox.next_sequence(q.case_id)
        if new_docs:
            await outbox.enqueue(
                q.case_id,
                "claim.documents",
                "POST",
                f"/v1/hospital-api/claims/{q.claim_ref}/documents",
                body=[presign(d) for d in new_docs],
                idem=uuid5(NS, f"{q.id}:docs:{resp.version}"),
                sequence=seq,
            )
            seq += 1
        await outbox.enqueue(
            q.case_id,
            "query.response",
            "POST",
            f"/v1/hospital-api/queries/{q.insurer_query_id}/responses",
            body=QueryResponse(
                query_id=q.insurer_query_id,
                answer_text=resp.draft_text,
                attached_doc_ids=resp.attached_doc_ids,
                responded_by=user.display_ref,
            ),
            idem=uuid5(NS, f"{q.id}:resp:{resp.version}"),
            sequence=seq,
        )
        await responses.mark_sent(resp)
        await queries.mark_answered(q)
        if not await queries.any_open(q.case_id):
            await transitions.apply(case, "acknowledged", actor=user)
        await audit.append(q.case_id, "query.sent", {"round": q.round, "version": resp.version})
    await events.publish("query.answered", q.case_id, {"query_id": q.id})
```

### 5.5 Step detail: SLA reminders (task 10)
For a query with `received_at = t0`, `due_by = t1`, `span = t1 − t0`: reminder `fire_at = t0 + span × {0.5, 0.8, 1.0}`. Each reminder fires an n8n flow (doc 08) → notification to `assigned_to` (or role queue) and SSE `query.overdue` at 100%. Reminders cancelled on `answered`/`closed`. Example: received 2026-10-06 10:00, due 2026-10-09 10:00 (72 h) → reminders at 10-07 22:00, 10-08 19:12, 10-09 10:00.

### 5.6 Step detail: SSE hub (task 11)
```python
class Hub:
    async def publish(self, ev: Event):
        entry_id = await redis.xadd(
            "hosp.events.stream", {"e": ev.json()}, maxlen=10_000, approximate=True
        )
        ev.id = entry_id
        await redis.publish("hosp.events", ev.json())

    async def subscribe(self, user, scope, case_id, since):
        if since:  # resume
            for eid, fields in await redis.xrange("hosp.events.stream", min=f"({since}", max="+"):
                ev = Event.parse(fields["e"])
                if await self.allowed(user, ev, scope, case_id):
                    yield ev
        async with redis.pubsub() as ps:
            await ps.subscribe("hosp.events")
            async for msg in ps.listen():
                ev = Event.parse(msg["data"])
                if await self.allowed(user, ev, scope, case_id):
                    yield ev
```
`allowed()` = `case_scope(user, ev.case_id)` (Desk: cases they created or are assigned; Officer: all in their department; Admin: none of the case data events, only system events). Events carry **no PII** — ids, statuses and counts only; the UI refetches detail through authorised REST. If `since` is older than the stream's trim point, the server sends `event: reset` so the client refetches everything.

## 6. Key logic
```python
async def on_query_callback(q: QueryIn):
    async with db.begin():
        if not await inbound.insert_if_new(...):
            return Replay
        case = await cases.by_ref(q.claim_ref, lock=True)
        row = await queries.upsert(case, q)
        await transition_if_needed(case, "under_query")
        await audit.append(case.id, "query.received", {"round": q.round, "category": q.category})
    await events.publish("query.new", case.id, {"query_id": row.id, "round": q.round})
    await n8n.trigger("query/intake", {"query_id": row.id}, idem=str(row.id))
```
SSE generator:
```python
async def stream(request, user, last_id):
    async for ev in hub.subscribe(user, since=last_id):
        if await request.is_disconnected():
            break
        yield f"id: {ev.id}\nevent: {ev.type}\ndata: {json.dumps(ev.data)}\n\n"
```

### 6.1 Ticket flow
```python
@router.post("/v1/stream/ticket")
async def ticket(body: TicketReq, user=Depends(current_user)):
    t = "tk_" + secrets.token_urlsafe(24)
    await redis.set(
        f"sse:ticket:{t}",
        json.dumps({"uid": str(user.id), "scope": body.scope}),
        ex=settings.sse_ticket_ttl_s,
    )
    return {"ticket": t, "expires_in": settings.sse_ticket_ttl_s}


@router.get("/v1/stream")
async def stream_ep(
    request: Request, ticket: str, scope: str = "inbox", case_id: UUID | None = None
):
    data = await redis.getdel(f"sse:ticket:{ticket}")  # atomic one-time use
    if not data:
        raise Problem(401, "invalid_ticket")
    user = await users.load(json.loads(data)["uid"])
    await limiter.enforce(user.id, max_conns=5)  # closes the oldest connection beyond 5
    return StreamingResponse(
        sse_gen(request, user, scope, case_id, request.headers.get("last-event-id")),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
```
Reconnect: browsers' `EventSource` auto-reconnects but the ticket is spent, so the UI client wrapper (doc 10) requests a fresh ticket in `onerror` and builds a new EventSource with `Last-Event-ID` passed as `?last_id=` (server prefers header, falls back to the param).

## 7. Config / env vars
`HOSP_SSE_HEARTBEAT_S=15`, `HOSP_SSE_STREAM_MAXLEN=10000`, `HOSP_SSE_TICKET_TTL_S=30`, `HOSP_QUERY_REMINDER_PCTS=50,80,100`; config `query_policy`: `max_rounds=3`, `default_sla_hours=72`, `hospital_round3_two_person`.

## 8. Error handling and edge cases
- Duplicate or out-of-order query callbacks: sequence/idempotency table ignores them.
- Query references unknown claim: 404 `unknown_claim`, no state changed.
- Round 4 attempt from insurer: 422 `max_rounds_exceeded`; audit anomaly.
- Draft hallucination: grounding guard blocks; officer can still write manually; never auto-send (human authority principle).
- Reply sent twice: idempotency key = uuid5(query_id, response_version).
- Insurer asks for a doc we do not have: create `doc_request` and tell desk; response may be partial ("will follow") — template text, counts against SLA.
- Proxy buffering breaks SSE: `X-Accel-Buffering: no`, `Cache-Control: no-cache`; Next.js route handler proxies stream untouched.
- Many tabs: cap 5 connections per user; oldest closed.
- Redis down: SSE returns 503, UI falls back to polling every 10 s.
- PII: query text may contain patient data; stored as-is in DB but masked before external LLM calls (crew job does masking via doc-pipeline Presidio; API passes only masked context).

### 8.1 Edge-case table
| Situation | Behaviour |
|---|---|
| Round 2 arrives while round 1 still `draft_ready` | round 1 marked `closed` (superseded by insurer) with audit; round 2 handled fresh; any unsent draft archived |
| Same `insurer_query_id` arrives with different text (insurer edit) | `UPSERT` updates text, bumps `revision`, clears approvals, notifies Officer |
| Officer approves, then insurer closes the query before send | `send` returns 409 `query_closed`; response archived |
| Two Officers send at the same time | row lock + `already_answered` guard |
| Attachment doc still scanning | send blocked 409 `attachment_not_ready` listing doc ids |
| Round 3 response requires a doc not obtainable | template partial response available; sets `partial_response=true` and creates a follow-up `doc_request` |
| Crew draft arrives after human already wrote v1 | stored as next version (`source='agent'`), does not overwrite; UI shows "new suggestion available" |
| Heavy burst of events (500/s) | stream trimmed to 10k; slow clients disconnected when server-side queue > 1000 events (client receives `reset` on reconnect) |
| EventSource through corporate proxy that buffers | fallback long-poll `GET /v1/events?since=` (same authorisation) every 10 s |
| Time zone display | server sends UTC; UI renders IST |
| Query text > 20 KB | truncated for display with "show more"; full text stored; crew receives chunked/summarised context |

## 9. Tests
Callback idempotency/order; round progression 1→3 and 4th rejected; triage fallback when crew down; grounding guard (fake citations, altered numbers, valid quotes); approval/two-person rule on round 3; send → outbox → tpa-sim receives once; SLA reminders timing (freezegun); SSE: connect, receive, resume with Last-Event-ID, authorization filtering (Desk can't see others' cases), heartbeat, ticket one-time use; load test 200 SSE clients; end-to-end with scripted tpa-sim query generator over 3 rounds.

### 9.1 Grounding guard test cases
| # | Draft content | Cited source | Expected |
|---|---|---|---|
| G01 | "troponin 4.8 ng/mL" | page contains "troponin 4.8 ng/mL" | ok |
| G02 | "troponin 5.8 ng/mL" | page says 4.8 | unsupported number `5.8` |
| G03 | quote not in page | — | `quote_not_found` |
| G04 | citation doc from another case | — | `doc_not_in_case` |
| G05 | "amount ₹1,80,000" | case facts claimed 180000 | ok (normalised) |
| G06 | "we guarantee approval" | — | `forbidden_commitment` |
| G07 | date "28/09/2026" vs page "28-Sep-2026" | page | ok (date normalisation) |
| G08 | quote with OCR noise (1 char off) | page | ok via fuzzy ≥ 90 |
| G09 | no citations at all, but numeric claims | — | each number unsupported |
| G10 | draft with only polite text, no facts | — | ok (nothing to ground) |

### 9.2 SSE tests
- **Auth**: no ticket → 401; used ticket → 401; expired (>30 s) → 401; ticket for scope `inbox` cannot subscribe to another user's `case_id` → 403.
- **Delivery**: publish after commit only (rollback test publishes nothing); event reaches client in < 2 s p95 on local compose.
- **Resume**: disconnect, publish 5 events, reconnect with `Last-Event-ID` → exactly the 5 events, in order, no duplicates; trimmed id → `reset` event.
- **Heartbeat**: comment line every 15 s (±1 s); intermediary-timeout simulation.
- **Limits**: 6th connection closes the 1st; 200 concurrent clients receive same event within 2 s; memory stable over 10 minutes.
- **Authorisation filtering**: Desk A cannot receive events for Desk B's case; Admin receives none of the case events.

### 9.3 End-to-end 3-round script (with tpa-sim scripted query generator)
1. Case acknowledged. tpa-sim emits Q1 (medical_clarification). Expect: `under_query`, triage `medical/officer`, draft within 60 s (fake crew), grounding ok.
2. Officer edits, approves, sends. tpa-sim receives once; status `acknowledged`.
3. tpa-sim emits Q2 (billing_discrepancy) 5 min later. Repeat. Answer needs a new doc → upload → attachment scan → send docs + response (order preserved).
4. tpa-sim emits Q3 (policy_exclusion). Expect `escalation_risk=true`, urgent, two-officer approval; first approver cannot be second approver.
5. tpa-sim emits Q4 → hospital returns 422 `max_rounds_exceeded`, anomaly in audit.
6. Verify audit chain end-to-end and exactly 3 `query.sent` events.

### 9.4 Event catalogue (payload `data` shapes; no PII)
| Event | `data` | Emitted by | UI reaction |
|---|---|---|---|
| `case.status_changed` | `{status, previous}` | transitions service | update badge, step bar |
| `case.route_changed` | `{seq, changed:["flags","required_steps"]}` | router (doc 05) | refetch route |
| `doc.uploaded` / `doc.scanned` / `doc.parsed` / `doc.classified` | `{doc_id, doc_type?, state}` | doc 03 pipeline callbacks | update document row |
| `completeness.updated` | `{run_no, complete, blockers}` | doc 04 | refetch checklist |
| `claim.draft_ready` / `claim.validation_failed` | `{version, errors?, warnings?}` | doc 06 | open review pane / banner |
| `claim.signoff_invalidated` | `{reason}` | doc 06 | disable submit |
| `submission.sent` / `.failed` / `.dead` | `{outbox_id, attempts}` | outbox worker | status chip, retry button |
| `query.new` / `.draft_ready` / `.overdue` / `.answered` | `{query_id, round}` | this doc | inbox badge, toast |
| `decision.received` | `{outcome}` | doc 06 | decision panel |
| `settlement.received` | `{amount}` | doc 06 | settlement panel |
| `reset` | `{}` | hub | refetch everything |

### 9.5 Permission matrix (query endpoints)
| Action | Desk | Officer | Admin |
|---|---|---|---|
| View inbox / query | own cases | department | none (no case data) |
| Override triage | no | yes | no |
| Request draft | yes | yes | no |
| Edit response | yes (draft only) | yes | no |
| Approve | no | yes | no |
| Send | no | yes (second approver on round 3) | no |
| Upload supplementary docs | yes | yes | no |
| Subscribe to SSE | own scope | department scope | system events only |

## 10. Acceptance criteria
- [ ] A scripted 3-round query exchange completes with human approvals at each round and full audit trail.
- [ ] Ungrounded draft is flagged and cannot be approved without edit or explicit override note.
- [ ] UI receives live updates < 2 s after server event; resume works after reconnect.
- [ ] No message is sent without an approved response version.

## 11. Dependencies
Docs 01-03, 06 (outbox, callbacks), 08 (n8n flows), 09 (crew); contract 01-01/01-02; tpa-sim scripted query generator (Dev B).

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/07-api-query-inbox-sse.md. Start with SSE hub + ticket auth (tasks 11-14) so the UI can integrate early, then the callback receiver, triage fallback, draft/grounding guard, approval and send.
