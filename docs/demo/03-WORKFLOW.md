# Vortex — The workflow in detail (one claim, start to finish)

This follows **one claim** through every system. For each stage you get: *who acts*, *what triggers it*, *the code that does it*,
*what changes in the databases*, and *what you can show*. Read after `01-RUNBOOK.md` and `02-HOW-IT-WORKS.md`.

Two status ladders move in step (hospital on the left, insurer on the right):

```
HOSPITAL status                         INSURER status
draft ─► docs_pending ─► docs_complete  (does not exist yet)
   ─► building_claim ─► ready_for_review
   ─► submitted ─────────────────────►  received ─► verifying ─► ready_for_decision
   ─► acknowledged ◄─────────────────────────────────────────────────────┘
   ─► under_query ◄──────────────────►  needs_info (and back through verifying)
   ─► approved / partially_approved / rejected ◄── awaiting_approval ─► approved / partially_approved / rejected
   ─► settled ◄──────────────────────── settled
   ─► closed                            closed
```
(The mapping is `INSURER_TO_HOSPITAL` in `contract/python/claim_contract/enums.py`.)

---

## PART A — Hospital side: from a patient to a signed claim

### A1. A case is created (front desk)
- **Who/when:** `desk1` creates a case in the hospital UI (or the demo script does).
- **Call:** `POST /v1/cases` → `hospital/api/app/routers/cases.py` → `services/cases.py`.
- **What happens:**
  1. Role check (deny-by-default guards) and an `Idempotency-Key` so a double-click does not make two cases.
  2. The patient, the policy reference (insurer name, policy number, member id) and the admission details are saved.
  3. The database function `next_claim_ref()` gives the number **HC-2026-000123**.
  4. The **router engine** (`router_engine/service.py`) decides **cashless vs reimbursement** and which documents are required
     (config `router_rules`, `doc_requirements`).
- **DB:** rows in `claim_case`, `patient`, `insurance_policy_ref`; status **draft**; first rows in `case_status_history` and `audit_event`.
- **Show:** `select claim_ref, status from claim_case order by created_at desc limit 3` (command in the runbook).

### A2. Documents are uploaded
- **Call:** `POST /v1/cases/{id}/documents` → `routers/documents.py` → `services/documents.py` and `filechecks.py`.
- **Checks, in order:** file type by content (PDF/JPEG/PNG/TIFF, not just the extension), size, page limits, then the **ClamAV virus
  scan** (port 3310). Infected files are quarantined.
- **Storage:** the file goes to **MinIO** bucket `hospital-docs` (versioned, encrypted). A row in `document` records the hash,
  size, pages, scan result.
- **Status:** the case moves **draft → docs_pending**. An `audit_event` is appended.
- **Trigger of the next stage:** the API calls the hospital n8n webhook `intake/document-uploaded` with a secret header
  (`services/n8n.py`).

### A3. n8n reads every document (flow `hosp_f1_intake`, port 5688)
n8n only sequences; the API holds the truth.

1. n8n verifies the webhook secret, answers 200 immediately, and drops duplicates (`util-idempotency` → `POST /v1/internal/idempotency`,
   a Redis `SET NX`).
2. Asks the API for the document (`GET /v1/internal/documents/{id}`) — only clean files continue.
3. **Vision service** (`:8300`, `services/vision-service/vision/`): page quality (blur, contrast, skew, size), **stamp detection**
   (a trained random-forest on candidate regions, `stamp_model.json`), registry match. Blocking quality problems → status `needs_review`.
4. **Document pipeline** (`:8200`, `services/doc-pipeline/docpipe/pipeline.py`):
   - get the text: PDF text layer, or **OCR** (Tesseract) for scans;
   - **classify** the document (prescription, pharmacy bill, final bill, discharge summary, …);
   - **mask personal data** with Presidio + own recognisers (Aadhaar, PAN, phone, e-mail) — only masked text may ever reach a cloud
     model, identity documents stay local;
   - **extract fields** with the local model in **two passes**, then check that each value really appears in the text
     (evidence check); printed table totals override model guesses; medicines and ICD codes are filled by code.
5. n8n posts each pass to the API (`document_parse` rows) and the classification (`document.doc_type`, confidence).
- **DB:** `document_parse`, `document` (type, `parse_status` = parsed / needs_review / failed).
- **Show:** the n8n website (runbook doc 04) → workflow `hosp-intake-document` → Executions.

### A4. Is the file complete? (the API evaluates, n8n just notifies)
- **Code:** `hospital/api/app/completeness/rules.py` (+ `service.py`), config from `config_set`/`config_version`
  (`doc_requirements`).
- **Default required set (a product decision):** prescription + pharmacy bill + final bill; plus a **procedure bill** for surgery and
  an **implant sticker** for implants. Bills must carry the **hospital stamp**; parse confidence must be ≥ 0.80.
- **Result:** one line per rule (`present_ok`, `missing`, `unusable`, `needs_review`, `waived`). A manager can **waive** a rule with a
  reason (`requirement_waiver`).
- **Status:** all rules satisfied → **docs_complete**. n8n flow `hosp_f2_completeness` pushes a live update to the UI over SSE
  (`sse/hub.py`, Redis stream `sse:hospital:events`).
- **DB:** `completeness_check` rows.

### A5. The claim is built (hospital crew)
- **Trigger:** an officer presses *Build claim*: `POST /v1/cases/{id}/claim/build` → `services/claim_builder.py`.
- **Context assembly:** the API gathers the typed fields of every document (`build-context`), status becomes **building_claim**.
- **Crew job** (`hospital/crew`, `:8010`): `agents/builder.py` assembles the contract-shaped claim: bill lines from the final
  bill/pharmacy bill, **categories** from `tools/categories.py` + `category_map.yaml` (room, icu, surgery, medicine, consumable, …;
  the model is asked only for lines no rule recognises), totals, diagnosis codes (from the discharge summary), source document and
  page for each line. Guards: `guards/pii.py` (no personal data in prompts), `guards/grounding.py` (every value traces to a document).
- **Validation:** `claim_validation/rules.py` runs rules V01–V12 (totals add up, line dates inside the stay, at least one ICD code,
  no duplicate line ids, stamp present, …). Errors block; warnings need acknowledgement.
- **DB:** a new `claim_draft` (versioned) with `payload`, `validation`, `provenance`, `has_errors`; `bill_line` rows. Status
  **ready_for_review**.

### A6. Human review and sign-off
- Officer opens the draft, may edit (each edit is a new `claim_draft` version via `claim_edit.py`), acknowledges warnings, and
  signs off: `POST …/claim/signoff` → `signoff` row.

### A7. Submission — the reliable part
- **Call:** `POST /v1/cases/{id}/claim/submit` → `services/submission.py`.
- **In one database transaction:** final re-validation; build the contract `ClaimSubmission`; give each document a short-lived download
  link; write an **`outbox`** row (kind `claim.submit`, body, idempotency key, sequence); move the case to **submitted**; audit event.
- **Why an outbox:** a crash after the commit still sends; a crash before it sends nothing. Never half-way.
- **Delivery:** the background worker (`hospital/api/app/outbox/`) signs the request (HMAC headers) and posts
  `POST {insurer}/v1/hospital-api/claims`. Failures retry after 1 s, 4 s, 16 s, 60 s, 5 min, … (8 tries), then a dead letter an admin sees.
- **Watch:** n8n `hosp_f4_submission_watch` raises a banner if no acknowledgement arrives in 15 minutes.

---

## PART B — Insurer side: receive, verify, calculate

### B1. The door (`insurer/api/insurer_app/`)
- **Code path:** `ContractAuthMiddleware` (from `claim_contract/insurer_side/middleware.py`) → `routers/hospital_api.py` →
  `services/receipt.py` + `validators.py`.
- **Middleware checks:** known key id, **signature**, timestamp within ±5 min, rate limit, **idempotency** (a repeat returns the stored answer).
- **Door rules V1–V13:** supported contract version; admission/discharge dates sane; stay length; number and size of documents; the
  hospital is in the network, active and matches the key; **SSRF guard** — document links may only point at allowed hosts.
- **Result:** HTTP **202** with an acknowledgement `IC-2026-000045`. Bad input is a typed error (422 / 403 / 409).
- **DB:** `core.claim_case` (status **received**), `core.bill_line`, `core.claim_document`, `core.policy_member` link, a config snapshot
  (which config versions applied to this claim), audit event. The case is stored with the hospital's claim number so duplicates are rejected.
- **Callback:** a status update goes back to the hospital through the insurer's own outbox → hospital becomes **acknowledged**.

### B2. Fetching the documents
- `services/docs_fetch.py` downloads each document via the short-lived link, checks the SHA-256 against the claim, stores it in the
  insurer's bucket, and marks `fetch_status`. Expired links trigger `…/refresh-url` back to the hospital.

### B3. The verification run (six steps, in order)
- **Who sequences it:** with `INS_ORCHESTRATOR=n8n`, the insurer n8n flow **`10_verification_main`** (webhook `verification-start`);
  otherwise `services/orchestrator.py` does the same in-process. Either way the **API computes every result**
  (`services/verification.py`, rules in `verification/engine.py`); n8n/crew only add explanations.
- n8n flow detail: create run (`POST /internal/cases/{id}/runs`, idempotent) → for each step a sub-flow (`11_…` to `16_…`):
  `POST …/steps/{step}/start` → (agent call) → `POST …/steps/{step}/result` or `/evaluate` → follow the API's `next_step` →
  `POST …/runs/{id}/finalize`.
- **DB:** `core.verification_run`, one `core.verification_step` per step (status, score, findings, agent output).

| # | Step | The rules (code) | The AI part (crew) |
|---|---|---|---|
| 1 | **document_fetch** | all documents downloaded and hash-verified | — |
| 2 | **completeness** | required documents for this claim type/procedure present, stamped, parse confidence ≥ 0.80 (`doc_requirements` config) | — |
| 3 | **identity** | patient name vs member record (fuzzy score), date of birth, gender, policy/member numbers | `identity` agent explains variations (e.g. initials) |
| 4 | **authenticity** | bill total vs sum of lines (±1 INR), dates inside the stay, **duplicate claim** (same member + overlapping stay), duplicate document hash, stamp present, image-tamper score from vision | `authenticity` agent explains signals — cannot invent one |
| 5 | **coverage** | policy active and premium paid, member covered from, **waiting periods** (initial, pre-existing, specific), pre-existing conditions, exclusions, sum insured remaining | `coverage` agent finds the wording with **RAG** and quotes it |
| 6 | **calculation** | **calc engine** `S0…S12`: map lines to groups → exclusions → room-rent / ICU caps (percent of sum insured) → **proportionate deduction** → sub-limits → line caps → deductible → **co-pay** → sum-insured cap → invariants (payable ≤ claimed, deductions add up) | `calc_mapper` maps unknown line wording (inline mode) |

- A step that fails a rule produces **findings** with a code, severity (`info`/`warning`/`blocker`), evidence and whether the hospital
  can fix it. Later steps are skipped when an earlier one cannot pass.
- **Finalize** (`verification/outcome.py`): *blockers that the hospital can fix* → status **needs_info** (a query draft is created);
  otherwise **ready_for_decision**, and a **recommendation** (`core.decision`, kind `recommendation`) with the payable, deductions and reasons.
- A bill-level discount is netted so that *approved + deductions = claimed*.

---

## PART C — The decision

### C1. The gate (`services/gate.py`, `decision.py`)
Computed fresh whenever someone looks:

| Condition | Tier | Who must act |
|---|---|---|
| all clean AND amount ≤ **50,000** AND identity ≥ 0.90 AND not degraded | **auto** | nobody: the system approves (`decision.auto_approved` audit event) |
| amount > 50,000, or any warning/flag, or something degraded | **single approver** (or reviewer) | one reviewer submits, one approver signs |
| amount > **500,000** (rejections above it too) | **dual approver** | two different approvers, at least one senior |
| a failed gate | forced human | — |

(`gate_amount = max(claimed, payable)`; exactly on the limit counts as the lower tier.)

### C2. The human path
1. Reviewer opens the **workspace** (`GET /v1/cases/{id}/workspace`): documents, steps, findings, the calculation table, recommendation.
2. `POST …/decision/submit` with outcome (approve / partial / reject), amount, reason codes. Changing the recommendation needs a reason of
   at least 10 characters; a reviewer cannot raise the amount above the engine's payable (only a senior with a reason).
3. A **decision task** opens (`core.decision_task`); approvers vote (`POST /v1/decisions/{id}/approvals`) — **segregation of duties**: the
   submitter and each approver must be different people; for the dual tier one vote is not enough.
4. When enough votes arrive the decision is **final** → case **approved / partially_approved / rejected**.
- **DB:** `core.decision` (kind `final`), `core.approval` rows, audit events.

### C3. The hospital is told
- The insurer writes a **decision callback** to its `ops.outbox` and the dispatcher (`jobs_cron.dispatch_once`, every second) signs and
  sends `POST {hospital}/v1/insurer-callbacks/decisions` with a **sequence number**.
- Hospital side (`services/callbacks.py`): signature, idempotency, **sequence check** (old = ignored, gap = reconcile), state-machine check,
  then status **approved / partially_approved / rejected** and a live UI update.

---

## PART D — Questions between insurer and hospital

### D1. The insurer asks
- **Triggers:** verification ends in `needs_info` (missing/illegible document, identity doubt, billing discrepancy) or a reviewer writes one
  (`POST /v1/cases/{id}/queries`, `send:true`). A reviewer may ask even when the case is `ready_for_decision`.
- **Draft:** the `query_drafter` agent writes a polite, specific text (sentence per finding, deadline from config, no accusation, no
  promise); code lint checks length, allowed document types, no payable amounts. A human can edit before sending.
- **Round and deadline:** rounds **1–3**, each with a deadline from `query_policy` config.
- **Send:** `ops.outbox` → `POST {hospital}/v1/insurer-callbacks/queries`. Insurer case **needs_info**.

### D2. The hospital answers (n8n `hosp_f5a/b`, hospital crew)
1. Hospital stores it (`insurer_query`), status **under_query**, webhook `query/intake`.
2. Flow `hosp_f5a_query_intake`: **crew triage** classifies it (needs a document? an explanation? risky category?).
3. `hosp_f5b_query_draft` → hospital crew `agents/responder.py` drafts a **grounded reply** (every sentence traces to the claim file;
   `guards/grounding.py`; no personal data).
4. **Officer approval**: one officer for normal queries, **two** for risky categories or round 3; edits allowed.
5. Send: `POST {insurer}/v1/hospital-api/queries/{query_id}/responses` (signed; attachments are documents uploaded first).

### D3. The insurer reads the answer
- Stored in `core.query_response`; job `triage_response` (`services/queries.py`): the **triage** agent (or rules) decides *sufficient /
  partial / insufficient / off-topic*, with a code check of attached document types overriding the model.
- Sufficient or partial → the round closes and **re-verification** runs only the steps the new documents affect → back to
  `ready_for_decision`. Otherwise a reviewer is notified.

### D4. Round 3 and escalation
- If round 3 passes its deadline unanswered (or stays unresolved) → `core.escalation` is opened, case **escalated**, senior reviewers
  notified. A senior chooses: **decide now**, **request more**, or **reject for non-compliance**.

---

## PART E — Settlement (simulated money)

1. After approval the job `initiate_settlement` (`services/settlement.py`) runs: resolve the **payee** (the hospital's registered account hash;
   for reimbursement the member's), apply **adjustments** (pre-authorisation advance already paid is subtracted), create `core.settlement`
   (status `initiated`, its own idempotency key).
2. **Bank request:** `POST {tpa-sim}/bank/payouts` signed with the bank key (`clients/bank_sim.py`). The simulator (`services/tpa-sim/tpa_sim/bank.py`)
   answers "queued", then pays after a moment according to its *profile* (`always_pay`, `fail_once_then_pay`, `reverse_after_pay`, …).
3. **Bank callback:** `POST /internal/settlement/bank-callback` with a **UTR** number (`SIMUTR…`); amount and UTR are verified, the
   settlement becomes `paid`, the case **settled**, utilisation of the sum insured is updated.
4. **If the bank fails:** `failed` with a reason, `attempt_count` +1, `next_retry_at`; `retry_due()` tries again with a fresh idempotency key
   (up to 3 attempts), then a task for a human (`core.settlement_task`). Reversals and refunds are also handled.
5. **Hospital:** a settlement callback → hospital status **settled**, then **closed**.
6. **Safety:** `assert_sim_mode` — the service refuses to run with any mode other than simulation: no real money, ever.

---

## PART F — What runs in the background all the time

| What | Where | Does |
|---|---|---|
| hospital outbox worker | hospital API | delivers signed messages, retries |
| insurer dispatcher + jobs | insurer API (`main.py` lifespan) | callbacks, parked n8n triggers, due settlement retries every second; SLA tick every 5 min |
| n8n crons | hospital: stuck-document sweeper (2 min), reminders (5 min), query SLA (hourly), claim-build rescue (5 min). insurer: due queries + outbox kick (5 min), audit verification + housekeeping (nightly) | self-healing |
| SSE streams | Redis | live updates to the UIs |
| metrics | `/v1/metrics`, `/metrics` | Prometheus-style counters |

---

## PART G — The audit trail, end to end

Every stage above appends to a hash-chained audit log (hospital `audit_event`, insurer `audit.audit_event`). A claim's events read like a
story: `case.created → doc.uploaded → doc.scanned → completeness… → claim.built → claim.submitted → received → verification.run.completed →
decision.recommended → decision.finalised → callback.sent → settlement.initiated → settlement.paid → case.settled`. The last demo step runs
the **verify** endpoints on both sides and expects `ok`.

---

## The seven demo stories mapped onto this workflow

| Story | Path through the stages |
|---|---|
| `auto` | A → B → C1 says **auto** → C3 → E |
| `reviewer` | A → B → C1 single approver → C2 → C3 → E |
| `dual` | claim > 500,000: C1 dual → two approvals → E |
| `reject` | C2 outcome reject → C3 (hospital `rejected`), no E |
| `queries` | D1–D3 twice, D4 escalation, senior decides, C2 → E |
| `callbacks` | after E: replay a stale status, replay the same message, send a bad signature → hospital stays **settled** |
| `bank_retry` | E with a failing first payout → retry → settled |
