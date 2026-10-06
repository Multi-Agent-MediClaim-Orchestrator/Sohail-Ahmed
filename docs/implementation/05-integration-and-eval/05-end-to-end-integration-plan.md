# 05-05 — End-to-End Integration Plan

Status: PROPOSED. Joint. Defines integration milestones, environments, scripted scenarios with step-by-step expected outcomes on both systems, resilience procedures, the demo script and acceptance checklists. Scenario ids (S01-S26) come from `05-01-synthetic-data.md`.

## 1. Goal
Bring two independently built systems together with minimal surprise: staged integration against simulators first, then against each other, then full-corpus runs with chaos. Every scenario has explicit expected results so a failure is unambiguous.

## 2. Inputs / Outputs
Inputs: contract v1 (frozen end of Phase 0), synthetic corpus and golden set, `tpa-sim`, `hospital-sim`, compose profiles, evaluation targets.
Outputs: `tests/e2e/` suite, scenario runner `eval/suites/e2e.py`, `ops/demo-script.md`, signed-off checklist per milestone (section 12), defect log `docs/implementation/05-integration-and-eval/defects.md`.

## 3. Integration strategy

### 3.1 Four levels
| Level | Hospital side | Insurer side | Purpose |
|---|---|---|---|
| L0 Contract | schemathesis vs insurer-api OpenAPI | schemathesis vs hospital callbacks | wire compatibility |
| L1 Simulated peer | real hospital stack | `tpa-sim` (Dev B) | Dev A unblocked from day 1 |
| L1' Simulated peer | `hospital-sim` (Dev A) replaying `ClaimSubmission` from corpus | real insurer stack | Dev B unblocked |
| L2 Real↔real | real | real, `tpa-sim` only as scripted human stand-in for queries | milestone M2 |
| L3 Full corpus + chaos | real | real | milestone M4 |

### 3.2 Simulators
- `tpa-sim`: validates signature, returns Acknowledgement, then follows a script from `expected/queries.json` (delays, query rounds, final decisions). Small UI to override. Idempotent, sequence-numbered callbacks.
- `hospital-sim`: posts corpus claims, answers queries from scripted answers, injects faults (duplicate sends, stale timestamps, out-of-order callbacks, expired URLs).
- Both import `claim_contract` for signing and models (no re-implementation).

### 3.3 Handover rule
A feature is "integrated" only when its L0 test passes in CI and the scenario that exercises it passes at the highest level available (L1 or L2). Simulators are updated in the same PR as contract changes.

## 4. Environments

| Env | Where | Config |
|---|---|---|
| `dev-A`, `dev-B` | own machines | own profile; peer = simulator |
| `integ` | one machine, rotating | `make up-all` with `.env.integ`; `HOSP_PEER_INSURER_URL`, `INS_PEER_HOSPITAL_URL` |

Resource note: `up-all` is 23 containers. On 16 GB use `LLM_MODE=remote-only` (no large local model), parser concurrency 1, no `obs` profile; run heavy corpus eval in batches. If one machine is not enough, run hospital on machine A and insurer on machine B over LAN with mkcert certificates and the peer URLs pointed at LAN IPs.

### 4.1 Environment bring-up checklist (integ)
1. `git pull` both devs on the same commit; `make contract-test` green.
2. `make reset-data && make seed` (both systems, Keycloak realms, KB ingestion).
3. `make up-all`, wait for `make doctor` all `ready`.
4. `make check-secrets` shows matching fingerprints in both directions.
5. `make ping-peers` ok both ways.
6. `make warm` (loads models, first LLM calls).
7. Run `make e2e SCENARIO=S01` as a smoke test.

## 5. Milestones and checklists

| ID | When | Exit criteria |
|---|---|---|
| M0 | end Phase 0 | Contract v1 frozen; L0 tests pass on stubs; compose infra up; both devs can run `make up-infra` |
| M1 | end Phase 1 | A: case create → upload → scan → parse → classify works on S01-S03; B: receive signed claim from hospital-sim, store, calc-engine unit tests green; submit/ack path passes L1 on both sides |
| M2 | end Phase 2 | First real end-to-end claim (S01): upload to approval recommendation, human approves, decision callback received, hospital shows `approved`. Query loop not required |
| M3 | end Phase 3 | Query loop real↔real (S19, S20), UIs complete for both journeys; S01-S16 and S19-S22 pass |
| M4 | end Phase 4 | Full corpus eval + chaos; S01-S26 meet targets; demo rehearsed twice; defects triaged |

### 5.1 M0 checklist
- [ ] `claims-v1.yaml` merged, schemathesis passes against both stubs
- [ ] signing test vectors identical on both implementations
- [ ] compose `infra` profile healthy on both machines
- [ ] ownership table (00-README) agreed; branch protection set
- [ ] `X-Journey-Id` decision recorded (contract 1.0 vs 1.1)

### 5.2 M1 checklist
- [ ] Hospital: S01-S03 documents reach `classified`; completeness for S05 returns the exact missing type
- [ ] Hospital: submission to `tpa-sim` returns Acknowledgement; outbox retry demonstrated
- [ ] Insurer: `hospital-sim` submission accepted, stored, idempotent on replay (same key → replay header)
- [ ] Insurer: invalid signature, stale timestamp rejected with correct problem codes
- [ ] calc-engine unit tests green; reference calculator agreement ≥ 99% on available archetypes

### 5.3 M2 checklist
- [ ] S01 passes unattended 3 runs in a row on `integ`
- [ ] Audit chain verifies on both systems after the run
- [ ] Langfuse sessions exist for both systems with all LLM calls
- [ ] No C2/C3 data in gateway egress capture
- [ ] Decision callback and status mapping correct (01-02 section 4.3)

### 5.4 M3 checklist
- [ ] S19 resolves in round 2; S20 escalates in round 3; round 4 rejected with 409
- [ ] Both UI golden paths pass Playwright (hospital S01, insurer S01, query loop)
- [ ] S11-S16 amounts equal reference_calc exactly
- [ ] RS1-RS3 pass
- [ ] Reconciliation job runs and reports zero mismatches on a clean run

### 5.5 M4 checklist
- [ ] 500-case corpus run complete; metrics vs targets in `02-evaluation-harness.md` recorded
- [ ] RS1-RS10 pass or documented deviation
- [ ] zero open S1 defects
- [ ] runbook drills R1-R10 done
- [ ] demo rehearsed twice end-to-end with no manual DB edits
- [ ] fresh-machine reproduction in ≤ 60 min (excluding downloads)

## 6. Test conventions

### 6.1 Layout
```
tests/e2e/
  conftest.py            # stack fixtures, service tokens, case factory from corpus
  scenarios/test_s01_clean.py ... test_s25_pii.py
  resilience/test_rs1_insurer_down.py ... test_rs10_restore.py
  ui/ (playwright) test_hospital_golden.py test_insurer_golden.py test_query_loop.py
  helpers/ (wait_for_status, assert_audit_chain, assert_no_pii_egress, fetch_trace, dump_diagnostics)
```

### 6.2 Actors and tokens
Common actors: `desk1`, `officer1` (hospital); `reviewer1`, `approver1`, `approver2`, `admin_i` (insurer). Tokens are obtained from Keycloak with the password grant in test mode only (`TEST_MODE=true`), never in prod-like runs.

### 6.3 Waiting strategy
Poll status with exponential backoff (1 s → 10 s) up to the scenario SLA (default 15 min, parser-heavy 25 min). On timeout fail with a diagnostic dump: statuses on both sides, last 50 log lines per service filtered by `journey_id`, outbox state, trace links. Never use fixed sleeps. UI tests wait on SSE events.

### 6.4 Assertion helpers
`assert_status(system, case, expected)`, `assert_decision(case, outcome, payable, deductions)`, `assert_audit_chain(system, case)`, `assert_events_in_order(system, case, [...])`, `assert_no_pii_egress(since)` (gateway egress capture in `03-privacy-and-security.md`), `assert_langfuse_session(journey_id, min_generations)`, `assert_no_event(system, case, type)`.

### 6.5 Idempotent scenarios
Each test creates fresh cases with unique synthetic identities from the corpus manifest and cleans nothing up (data reset is a separate step), so tests can run in any order and in parallel up to the resource limit (default parallelism 1 on 16 GB).

## 7. Scenario catalogue with step-by-step expectations
Run with `make e2e SCENARIO=Sxx`. Format per scenario: Setup · Steps with expected result per system · Assertions · Audit events.

### E2E-S01 Clean cashless planned (golden path)
Setup: corpus case S01-0001, policy in force, preauth simulated, payable below `T_auto`.

| # | Actor/System | Action | Expected |
|---|---|---|---|
| 1 | desk1 / hospital | create case (planned, cashless) | status `draft`; audit `case.created`; config versions recorded |
| 2 | desk1 | upload 8 documents | each: `doc.uploaded`, `doc.scanned` clean, `doc.parsed`, `doc.classified` with correct `DocType`; SSE events visible |
| 3 | hospital | completeness | `complete=true`, `missing=[]`; status `docs_complete` |
| 4 | hospital crew | Claim Builder | `ClaimSubmission` built; totals reconcile; status `ready_for_review` |
| 5 | officer1 | review, no edits, sign off | audit `human.signoff`; status `submitted`; outbox row created and delivered |
| 6 | insurer | receipt | signature verified; `received` → Acknowledgement returned; hospital `acknowledged` |
| 7 | insurer | verification | Identity ok; Authenticity no flags; Coverage in force, no exclusions; Calculation payable P with trace id |
| 8 | insurer | decision gate | all hard gates pass and P ≤ `T_auto` → system auto-approves: state `ready_for_decision` → `approved`/`partially_approved`, actor=`system`, audit `decision.auto_approved` (no human task). If P > `T_auto` or any gate/flag fails → `awaiting_approval` and the agent asks a human |
| 9 | (auto) / approver1 if human needed | approve | `approved`; audit `decision.auto_approved` (auto path) or `human.approved` with distinct actor ids (human path) |
| 10 | insurer → hospital | Decision callback | hospital `approved` with `Decision` stored; sequence monotonic |
| 11 | insurer | settlement (simulated) | `settled`; hospital notified `settled` |
Assertions: both audit chains verify; Langfuse sessions have all LLM calls; `assert_no_pii_egress`; payable equals `reference_calc`; every decision record contains all version fields.

### E2E-S02 Clean cashless emergency
Same as S01 but admission type `emergency`: FIR/MLC not required, preauth may be post-hoc. Expect routing flag `emergency=true`, shortened SLA timers, no needs-info for absent FIR.

### E2E-S03 Clean reimbursement planned
Claim type `reimbursement`: required docs include cancelled cheque, payment receipts. Expect router `claim_type=reimbursement`, filing-window check against `deadlines` config passing, no preauth requirement, settlement pays to patient bank (simulated).

### E2E-S04 Reimbursement late filing
Discharge older than filing window. Hospital: completeness complete but a `deadline` warning shown with config version. Officer may submit. Insurer: applies late-filing rule from policy rules; outcome per policy (reject or partial with condonation reason) with clause citation; human confirms. Assert reason code and clause id.

### E2E-S05 Missing required document
| # | Step | Expected |
|---|---|---|
| 1 | upload packet without `final_bill` | `docs_pending`; needs-info task naming exactly `final_bill`; **no submission and no rejection** |
| 2 | attempt sign-off | blocked 409 `invalid_transition` |
| 3 | upload missing bill | re-evaluation automatic; `docs_complete` |
| 4 | continue as S01 | approve path |
Assertions: `doc_requirements` version recorded in `completeness.evaluated`; needs-info visible in desk UI and as SSE event.

### E2E-S06 Missing conditional document
Itemised bill includes an implant line but no `implant_sticker`. Expect `docs_pending` with reason `conditional_rule:implant`; message names the rule; upload resolves.

### E2E-S07 Blurry/illegible document
Vision quality score below threshold: doc marked `needs_reupload` with reason `low_quality`, desk asked to rescan; case cannot be `docs_complete` until replaced or officer overrides with logged reason.

### E2E-S08 Missing stamp
Vision flags `stamp_present=false` on final bill. Completeness blocker per `doc_requirements` (stamp required). Officer cannot sign off until replacement uploaded, or records an override reason (audit `human.override`, claim flagged `stamp_override` visible to insurer reviewer).

### E2E-S09 Wrong patient document
| # | Step | Expected |
|---|---|---|
| 1 | upload packet with one lab report for another patient | hospital identity discrepancy warning (name/dob mismatch) |
| 2 | officer ignores and submits (allowed, logged) | `submitted` |
| 3 | insurer Identity agent | raises mismatch; status `needs_info`; Query R1 category `identity_mismatch` |
| 4 | hospital | `under_query`; Query Responder drafts answer; officer attaches corrected doc and sends |
| 5 | insurer | satisfied; verification resumes; decision |
Assertions: query has `requested_doc_types`; audit `query.draft_generated`, `query.sent`.

### E2E-S10 Total mismatch
Claim Builder reports blocker `totals_mismatch` (code check, never LLM). Submission impossible; UI lists offending lines. Officer corrects a line via structured edit (audit `human.edited` with before/after), claim rebuilds, resubmits. Assert direct API submission with mismatched totals is rejected 422 `totals_mismatch` (contract validation).

### E2E-S11 Room rent over cap
Calculation applies proportional deduction under `room_rent_cap`; decision `partial`; deductions list rule ids and clause citations; hospital receives `Decision` with `Deduction[]`; amounts equal reference_calc to the paisa. Hospital UI shows per-line deductions.

### E2E-S12 Co-pay and deductible
Policy with 10% co-pay and deductible. Calc trace shows ordering rule (documented in `07-calc-engine.md`); payable equals reference; decision `partial` with `copay` and `deductible` deductions.

### E2E-S13 Waiting period
Diagnosis excluded within waiting period. Coverage agent plus rules → recommendation `reject` with reason code and clause id; human must confirm (principle 3); hospital sees `rejected` with reasons.

### E2E-S14 Exclusion
Procedure code in policy exclusion list: code-based `reject` recommendation with clause; human confirms; hospital sees reasons.

### E2E-S15 Policy expired
Pure code check (policy end date < admission). Recommendation `reject` immediately after Identity/Coverage; no LLM call needed for the verdict (assert no generation in the Coverage step for the verdict). Human confirms.

### E2E-S16 Sum insured exhausted
Prior claims consume most of SI. Payable capped to the remaining SI; decision `partial`; reason `si_exhausted`; reference match.

### E2E-S17 Tampered amount
Authenticity flags `amount_edit` or arithmetic inconsistency; claim routed to `escalated` or to reviewer with a flag; **never auto-recommended approve**. Eval recall counted. Assert recommendation != `approve`.

### E2E-S18 Duplicate claim
Second submission containing a duplicate bill number (different `claim_ref`). Insurer flags `duplicate`, shows link to first claim; reviewer decides; assert it is not a replay of idempotency (different keys).

### E2E-S19 Query loop resolved in round 2
| # | Actor | Action | Expected |
|---|---|---|---|
| 1 | insurer (tpa-sim script or reviewer) | raise Query R1 `medical_clarification` | status `needs_info`; callback to hospital |
| 2 | hospital | receive | `under_query`; inbox item; Query Responder drafts grounded answer with citations (doc id, page) |
| 3 | officer1 | edit (optional), send | audit `query.draft_generated`, `query.edited`, `query.sent`; `QueryResponse` delivered |
| 4 | insurer | triage (script: unsatisfied) | R2 with more specific ask (round=2) |
| 5 | hospital | answer with extra document | delivered |
| 6 | insurer | satisfied | back to `verifying` → decision |
Assertions: exactly 2 rounds; every number/date in drafts appears in source docs (`grounded_numbers_ok`); SLA timers recorded; `human_edit_distance` score exists.

### E2E-S20 Query loop escalates on round 3
Scripted unsatisfactory replies in rounds 1-3 (or SLA breach). After round 3 unsatisfied → `escalated` with assignment to the escalation role; attempt to create round 4 → 409 `invalid_transition`. Human decides; hospital sees `under_query` until decision.

### E2E-S21 Above `T_four`: two approvers
Payable > `T_four`. Needs two distinct approvers. Negative checks: same approver twice → 409; reviewer who prepared the case as approver → 403/409. Both approvals audited with actor ids; decision issued only after the second.

### E2E-S22 Between `T_auto` and `T_four`
The agent asks one human Approver for the final-stage decision. Reviewer cannot approve alone; assert state `awaiting_approval` and no `decision.auto_approved` event.

### E2E-S26 Flag forces human below `T_auto`
Payable ≤ `T_auto` but a `review_required` flag (e.g. `fraud_warning`) or a failed gate is present: assert the system does NOT auto-approve, state `awaiting_approval`, human approves.

### E2E-S23 Handwritten notes
Parser confidence below gate: affected fields flagged for human verification; case cannot auto-complete those fields; officer fixes (audit); claim proceeds. Assert no LLM-invented values (fields remain null until human sets).

### E2E-S24 Large itemised bill (6 pages, 50 lines)
Parsing completes within SLA; all lines present with `source_page`; totals reconcile; same decision path as S01.

### E2E-S25 PII stress
Free-text with fake IDs and phones. Assert masking recall target, `assert_no_pii_egress` over gateway capture, logs free of C2/C3 patterns.

## 8. Resilience scenarios (procedures)

Fault injection tools: `toxiproxy` (profile `chaos`) between hospital-api↔insurer-api and between services and Redis/gateway; `docker stop/start/kill`; clock skew via `X-Test-Clock-Offset` honored only when `TEST_MODE=true`.

| ID | Fault | Procedure | Expected |
|---|---|---|---|
| RS1 | insurer-api down during submit | start S01; at step 5 `docker stop insurer-api`; wait 3 retries; `docker start insurer-api` | outbox retries with backoff, same idempotency key; exactly one claim at insurer; UI shows `submitting (retrying)` then `acknowledged` |
| RS2 | duplicate callback delivery | `hospital-sim`/toxiproxy duplicates Decision callback | hospital applies once; second returns idempotent replay; one `Decision` row |
| RS3 | out-of-order status callbacks | send sequence 5 before 4 | seq 4 ignored if lower than applied; final state correct; log `callback.stale_sequence` |
| RS4 | clock skew 10 min | set offset header on sender | 401 `stale_request`; alert fires; fix clock; outbox retry succeeds |
| RS5 | presigned URL expired before fetch | submit, delay insurer fetch > TTL (shorten TTL in test) | insurer requests refresh; completes without manual action |
| RS6 | LLM quota exhausted mid-run | gateway returns 429 for primary alias | fallback to next alias/Ollama or human-review state with `llm_unavailable`; no hung cases |
| RS7 | parser worker crash mid-document | `docker kill doc-pipeline` during parse | job retried; no duplicate document or entity records; doc eventually `parsed` |
| RS8 | n8n worker restart mid-workflow | restart worker during completeness loop | execution resumes/retries idempotently; no duplicate audit events |
| RS9 | Redis restart | `docker restart redis` during submit | idempotency also in DB for claims (no double submission); SSE clients reconnect with `Last-Event-ID` and catch up |
| RS10 | DB restore to earlier snapshot on one side | restore insurer DB to snapshot before decision | reconciliation job detects divergence and reports it; no silent overwrite; operator procedure R2/R9 documented |
| RS11 | MinIO unavailable | stop minio during upload | upload returns 503 with retry hint; no orphan DB rows |
| RS12 | Keycloak down | stop keycloak | logged-in sessions continue until token expiry; new logins fail clearly; services mark `ready` degraded only for auth refresh |

Each resilience test records: injection time, detection time (alert), recovery time, data-integrity checks (counts of claims, decisions, audit chain validity).

## 9. Reconciliation
Nightly job (hospital): for every case in `submitted..under_query`, call `GET /claims/{ref}` and compare the status mapping (01-02 section 4.3). Mismatch creates `reconciliation_issue` shown in Admin and increments `reconciliation_mismatch_total`. Insurer mirrors for callbacks it believes were delivered (checks delivery acks). Resolution actions: replay callback, resync status, mark manual. Test: induce divergence (RS10) and assert detection.

## 10. Demo script (target 12 min)
| Min | Section | Details |
|---|---|---|
| 0-1 | Architecture | two isolated stacks, one signed contract, humans in control |
| 1-4 | Hospital | desk uploads S01 packet; live SSE feed as docs parse; a blurry doc (S07) triggers re-upload request; completeness → Claim Builder → officer sign-off → submit |
| 4-7 | Insurer | reviewer workspace: identity, authenticity, coverage, calc trace; approver approves; settlement |
| 7-9 | Query loop S19 | grounded draft with citations; officer edits and sends; round 2 resolves |
| 9-10 | Guardrails | S17 tampered flagged; S21 needs two approvers |
| 10-11 | Trust | audit chain verification and Langfuse trace; eval report slide |
| 11-12 | Failure drill | stop insurer-api; show retry/outbox recovery |

Pre-demo checklist: `make reset-data && make seed-golden`; `make warm`; Ollama models pulled; LLM quota verified; both devs rehearse twice; recorded fallback video (PROPOSED); disable notifications; browser profiles logged in for each role; second screen with Langfuse and audit views.

Fallback plan: if the LLM provider fails mid-demo, switch `LLM_MODE=local-only` (documented in R3) or play the recording; if a service flaps, `make doctor` and restart that service only.

## 11. Defect management
Severity: S1 wrong money, PII leak, bypassed human gate; S2 stuck/incorrect state; S3 degraded UX or metric miss; S4 cosmetic. S1 blocks a milestone. Defect record: id, scenario, `journey_id`, trace links, owner (A/B/joint), expected vs actual, regression test added. Triage 15 min at each sync point; owners fix within the phase; regression test required before closing.

Defect template:
```
### D-012 [S2] Decision callback applied twice
Scenario: RS2   Journey: J-HC-2026-000451   Owner: A
Expected: one Decision row   Actual: two rows
Trace: <langfuse link>   Regression test: tests/e2e/resilience/test_rs2_duplicate_callback.py
Status: fixed in PR #xx
```

## 12. Acceptance criteria
- M2: E2E-S01 passes unattended three runs in a row; M3: S01-S16 and S19-S22 pass; RS1-RS3 pass.
- M4: S01-S26 and RS1-RS12 pass or have accepted documented deviations; zero S1 defects open; eval targets from `02-evaluation-harness.md` met or each miss has an owner and mitigation; demo rehearsed twice without manual DB edits.
- Reproducibility: fresh machine to green `make e2e SCENARIO=S01` using only README steps in ≤ 60 min (excluding model downloads).
- Every scenario has a test file and a link to its expected outcomes in this doc.

## 13. Dependencies and Claude Code kickoff prompt
Depends on: every component doc; most heavily 01-01 (contract), tpa-sim (Dev B builds) and hospital-sim (Dev A builds), 05-01 (corpus), 05-03 (egress capture), 05-04 (journey ids, doctor, outbox tooling).

> Implement docs/implementation/05-integration-and-eval/05-end-to-end-integration-plan.md: first the `tests/e2e` skeleton, helpers and S01 against the simulators available now (state which side I am: A builds hospital-sim and hospital-side asserts, B builds tpa-sim scripts and insurer-side asserts). Add scenarios in the order of milestones M1-M4, keep each test independent and idempotent, and fail with the diagnostic dump described in section 6.3.
