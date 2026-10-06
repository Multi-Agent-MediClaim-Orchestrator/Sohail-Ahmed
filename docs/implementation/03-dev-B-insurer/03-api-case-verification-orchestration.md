# 03-03 — insurer-api: Case Management and Verification Orchestration (2.2.2)

Owner: Dev B. Status: PROPOSED.
Depends on: 01-insurer-db, 02-api-claim-receipt, 08-crew-agents (agents), 07-calc-engine, 09-n8n-flows.

## 1. Goal
Own the lifecycle of an insurer case after receipt: assign work, run the verification pipeline (document fetch → completeness → identity → authenticity → coverage → calculation) as a tracked `verification_run`, persist every step's validated output, compute a recommendation, and move the case to `ready_for_decision` or `needs_info`. n8n drives sequencing; this API is the system of record and does all validation/persistence.

## 2. Inputs / Outputs
- Input: internal calls from n8n (service account), reviewer calls from insurer-ui (Keycloak JWT), crew results (Pydantic objects returned over HTTP to this API by n8n or invoked by this API).
- Output: `verification_run`, `verification_step`, `calculation_result`, a `decision(kind='recommendation')`, status transitions with callbacks to hospital, audit events.

## 3. Data model
Tables: `claim_case`, `verification_run`, `verification_step`, `calculation_result`, `decision`, `claim_document`. Finding shape (JSON in `verification_step.findings`):

```python
class Finding(BaseModel):
    code: str  # namespaced, e.g. "identity.dob_mismatch"
    severity: Severity  # info | warning | blocker
    message: str  # human text, no raw PII
    evidence: list[Evidence]  # [{doc_id, page, bbox?, field, value_masked}]
    fixable: bool  # can a query resolve it?
    suggested_query_category: QueryCategory | None
    suggested_doc_types: list[DocType] = []
```

Finding code catalogue (PROPOSED, extend in code `app/verification/codes.py`):
| Step | Codes |
|---|---|
| document_fetch | `doc.fetch_failed`, `doc.hash_mismatch`, `doc.virus_found`, `doc.url_expired` |
| completeness | `completeness.missing_required`, `completeness.low_parse_confidence`, `completeness.wrong_doc_type`, `completeness.unsigned_discharge` |
| identity | `identity.policy_not_found`, `identity.member_not_found`, `identity.name_mismatch`, `identity.dob_mismatch`, `identity.gender_mismatch`, `identity.id_hash_mismatch` |
| authenticity | `auth.stamp_missing`, `auth.signature_missing`, `auth.tamper_suspected`, `auth.bill_arithmetic`, `auth.duplicate_claim`, `auth.dates_inconsistent`, `auth.hospital_not_empanelled` |
| coverage | `coverage.policy_inactive`, `coverage.outside_period`, `coverage.waiting_period`, `coverage.pre_existing`, `coverage.exclusion`, `coverage.sub_limit`, `coverage.sum_insured_exhausted`, `coverage.not_network` |
| calculation | `calc.rule_conflict`, `calc.unmapped_line`, `calc.negative_payable` |

Step status rules: any `blocker` finding with `fixable=true` → step `flagged`; `blocker` with `fixable=false` → `failed`; only warnings → `passed` with findings; none → `passed`.

## 4. API/endpoints

### 4.1 Reviewer-facing (Keycloak JWT; roles `reviewer`, `senior_reviewer`, `approver`, `admin`)
| Method | Path | Notes |
|---|---|---|
| GET | `/v1/cases` | filters: status, assignee, hospital, claim_type, priority, sla_breach, text (claim no / member name trigram); cursor pagination; excludes `submission` blob |
| GET | `/v1/cases/{id}` | case header plus latest run summary |
| GET | `/v1/cases/{id}/workspace` | aggregate: submission, bill lines, documents (presigned GET URLs, 15 min), steps+findings, recommendation, queries, decisions, audit tail |
| POST | `/v1/cases/{id}/assign` | `{assignee}`; reviewer self-assign or senior_reviewer assign to others |
| POST | `/v1/cases/{id}/verification/rerun` | optional `{steps:[...]}`; creates new run `trigger=manual_rerun`; role reviewer+ |
| POST | `/v1/cases/{id}/findings/{finding_key}/override` | reviewer marks a finding as resolved/not applicable with reason (audited, requires comment ≥ 10 chars) |
| PATCH | `/v1/cases/{id}/priority` | senior_reviewer |
| GET | `/v1/cases/{id}/documents/{doc_id}/download` | short-lived redirect to MinIO presigned URL; audit `doc.viewed` |
| GET | `/v1/cases/{id}/runs` | list runs |

### 4.2 Internal (service account `svc-n8n-insurer`, scope `internal`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/internal/cases/{id}/runs` | start run: body `{trigger}`; returns `{run_id, steps:[...] , config_versions}`; idempotent per `(case, trigger, client_token)` |
| POST | `/internal/runs/{run_id}/steps/{step}/start` | marks running |
| POST | `/internal/runs/{run_id}/steps/{step}/result` | body = step result object (validated by step-specific schema); returns `{next_step, run_status}` |
| POST | `/internal/runs/{run_id}/finalize` | compute outcome (see §6.4) and transition case |
| GET | `/internal/cases/{id}/context` | normalized bundle for crews: submission (PII minimized), policy snapshot, member snapshot, config versions, prior findings, prior queries |
| POST | `/internal/cases/{id}/transition` | guarded transition `{to, reason}` using `assert_transition` |

Example result payload (identity):
```json
POST /internal/runs/0192.../steps/identity/result
{"step":"identity","agent":{"name":"identity_agent","version":"1.3.0","prompt_version":"identity-v4","trace_id":"lf_8f2..."},
 "deterministic":{"name_similarity":0.93,"dob_match":true,"gender_match":true,"id_hash_match":null,"policy_active_on_admission":true},
 "score":0.94,
 "findings":[{"code":"identity.name_mismatch","severity":"warning","message":"Name differs by initial","fixable":false,"evidence":[{"doc_id":"...","page":1,"field":"patient_name","value_masked":"R*** K*****"}]}],
 "agent_notes":"Discharge summary uses 'Ravi Kumar S', policy card 'Ravi Kumar'."}
```
Server re-computes the deterministic parts itself and rejects the result if the agent's `deterministic` block disagrees (principle: deterministic first, evidence over confidence; the agent only supplies extracted text and explanations).

### 4.3 Step result schemas (all steps)
Common envelope:
```python
class AgentInfo(BaseModel):
    name: str
    version: str
    prompt_version: str
    trace_id: str | None


class StepResultBase(BaseModel):
    step: StepName
    agent: AgentInfo | None  # None for pure-rule steps (completeness, calculation)
    score: float | None
    findings: list[Finding]
    agent_notes: str | None = None
    degraded: bool = False
```
Per-step `deterministic` blocks (server recomputes and compares):
| Step | `deterministic` fields | Compared by server |
|---|---|---|
| document_fetch | `{fetched:int, failed:int, hash_mismatch:int, infected:int}` | derived from `claim_document.fetch_status` |
| completeness | `{required:[DocType], present:[DocType], missing:[DocType], low_confidence:[doc_id]}` | exact set equality |
| identity | `{name_similarity, dob_match, gender_match, id_hash_match, policy_active_on_admission}` | similarity ±0.005 |
| authenticity | `{arithmetic_ok, dates_consistent, duplicate_claim_ids:[], hospital_empanelled, stamp_present, signature_present}` | exact; stamp/signature come from vision-service results stored on documents |
| coverage | `{policy_active, within_period, waiting_period_days_remaining, pre_existing_hit, exclusion_hits:[], network_status}` | exact |
| calculation | `{calc_result_id, payable, claimed, deductions:[...]}` | calc-engine output is authoritative; agent only supplies line mapping |

Result response: `200 {"accepted":true,"step_status":"flagged","next_step":"coverage","run_status":"running","warnings":["agent.disagrees_with_rules"]}`. If the server recomputation disagrees beyond tolerance it still returns 200 but stores `agent_output` unchanged, uses the server's `deterministic` block for gating, and adds the warning (§8).

### 4.4 Error codes specific to this router
| Code | HTTP | When |
|---|---|---|
| `run_in_progress` | 409 | starting a run while another is `running` |
| `step_out_of_order` | 409 | posting a result for a step whose prerequisites are not terminal |
| `step_already_recorded` | 409 | second result for same `(run, step)` with different body; same body → 200 idempotent |
| `invalid_step_result` | 422 | schema or semantic validation failed (details list) |
| `stale_etag` | 412 | reviewer mutation with outdated `If-Match` |
| `forbidden_role` | 403 | RBAC denial |
| `case_closed` | 409 | mutations on terminal cases |

### 4.5 Workspace response shape (`GET /v1/cases/{id}/workspace`)
```json
{"case":{"id":"...","insurer_claim_no":"IC-2026-000123","status":"ready_for_decision","priority":2,"sla_due_at":"...","etag":7,"assigned_reviewer":"u-123"},
 "submission":{"patient":{"full_name":"...","dob":"..."},"admission":{...},"totals":{...}},
 "bill_lines":[{"line_no":1,"description":"...","category":"room","amount":"12000.00","deduction":{"amount":"3000.00","rule_id":"room_rent_cap"}}],
 "documents":[{"id":"...","doc_type":"discharge_summary","pages":3,"fetch_status":"fetched","parse_confidence":0.93,"view_url":"https://minio.../presigned?...","superseded":false}],
 "run":{"id":"...","run_no":2,"status":"completed","steps":[{"step":"identity","status":"passed","score":0.94,"findings":[...]}]},
 "recommendation":{"outcome":"partial","approved_amount":"164200.00","gate_tier":"single","reason_codes":["coverage.sub_limit"],"explanation":"..."},
 "queries":[{"id":"...","round":1,"status":"answered","category":"missing_document"}],
 "decisions":[...],"overrides":[...],
 "audit_tail":[{"seq":41,"event_type":"verification.step.completed","ts":"...","actor_id":"identity_agent"}]}
```
Documents are signed per request; `view_url` TTL = `INS_PRESIGN_TTL_SECONDS`. `deduction` per line is joined from the latest `calculation_result.output`.

### 4.6 RBAC matrix
| Route group | reviewer | senior_reviewer | approver | admin | svc-n8n |
|---|---|---|---|---|---|
| GET cases/workspace | ✔ | ✔ | ✔ | ✔ | — |
| assign (self) | ✔ | ✔ | — | ✔ | — |
| assign (others) | — | ✔ | — | ✔ | — |
| rerun | ✔ | ✔ | — | ✔ | ✔ (internal) |
| override finding (non-identity) | ✔ | ✔ | — | ✔ | — |
| override identity/authenticity blocker | — | ✔ | — | ✔ | — |
| priority | — | ✔ | — | ✔ | — |
| `/internal/*` | — | — | — | — | ✔ |
| admin replay | — | — | — | ✔ | — |

## 5. Build tasks
1. Models/schemas `app/verification/schemas.py`: `StepResult` union per step, `Finding`, `Evidence`, `DeterministicBlock` per step.
2. `app/verification/codes.py` finding catalogue with default severity, fixable flag, default query category and text template ids.
3. `app/verification/engine.py`: pure functions per step computing deterministic outputs from context (no I/O): `check_completeness`, `check_identity`, `check_authenticity_rules`, `check_coverage`.
4. `services/verification.py`: `start_run`, `record_step_result`, `finalize_run`, uses repos and transactions; audit events `verification.step.completed` (one per step), `verification.run.completed`.
5. Context builder `services/context.py`: loads submission, policy, member snapshots, config versions (via `ConfigService.resolve`), pre-existing `findings`, prior queries; applies PII minimisation (no `id_proof_hash` unless needed by identity step).
6. Reviewer endpoints incl. workspace aggregation with eager loads and single presigned-URL signer.
7. Assignment algorithm (`services/assignment.py`) PROPOSED: least-loaded active reviewer with matching skill tags (`cashless`, `reimbursement`, `high_value`) and not on leave; round robin tiebreak; senior reviewer for `claimed > T_auto`.
8. SLA tracker job (Arq cron every 5 min): sets `sla_breach` flag in Redis/set and emits audit `sla.breached` when the SLA is exceeded (100%). No 50%/80% elapsed reminders or priority bumps (user decision).
9. Override endpoint with audit + effect: findings overridden are excluded from outcome computation but remain visible.
10. Transition service wrapping `assert_transition` plus hospital callback enqueue via outbox using the status mapping.
11. Re-run semantics: new run supersedes older, old findings kept; `latest_run_id` on case (add column in migration 0008, PROPOSED).
12. Config-change replay: admin dry-run endpoint `POST /v1/admin/replay` re-runs deterministic steps only against N historical cases and returns diff counts (feeds 03-config-versioning dry-run).
13. Tests and fixtures (§9).
14. Implement `evaluate` endpoint for pure-rule steps (§6.12) and have `engine.py` functions invoked from it; keep `result` endpoint only for agent-backed steps.
15. Implement `outcome_from(findings, score)` helper and the step-status rules of §3 in one place (`verification/outcome.py`) with exhaustive tests; every `check_*` returns through it.
16. Implement `finding_key = sha256(f"{step}|{code}|{evidence_ref}")` helper and wire the `finding_override` table from 01-insurer-db §3.5.
17. Implement ETag handling: `If-Match: "<etag>"` on reviewer mutations; return `ETag` header on GET; map `StaleDataError` to 412.
18. Implement the `INVALIDATES` map in `engine.py` and a `plan_rerun(case, changed_docs)` function returning the minimal step list; unit-test with every `doc_type`.
19. Presence service: Redis key `presence:{case_id}:{user}` TTL 30 s refreshed by UI heartbeat `POST /v1/cases/{id}/presence`; used by assignment and by the "also viewing" indicator.
20. Prometheus metrics: `verification_run_seconds`, `verification_step_total{step,status}`, `finding_total{code,severity}`, `agent_disagreement_total{step}`, `cases_open{status}`.
21. Generate the golden dataset loader `tests/golden/load.py` reading `data/eval/golden_claims.jsonl` (format defined in 05-integration-and-eval/01-synthetic-data.md) and compare outcomes in CI nightly.

## 6. Key logic / pseudocode

### 6.1 Step order and dependencies
```
document_fetch → completeness → identity ─┐
                              authenticity ┤→ coverage → calculation → finalize
```
Identity and authenticity run in parallel (n8n split). Coverage requires identity not `failed`; calculation requires coverage not `failed`. If a prerequisite `failed`, dependants are `skipped` with finding `step.skipped_due_to.<step>`.

### 6.2 Completeness (deterministic)
```python
def check_completeness(ctx) -> StepOutcome:
    req = ctx.cfg.doc_requirements.resolve(ctx.claim_type, ctx.admission_type, ctx.procedure_group)
    have = {
        d.doc_type for d in ctx.documents if d.fetch_status == "fetched" and not d.superseded_by
    }
    findings = []
    for t in req.required - have:
        findings.append(
            F(
                "completeness.missing_required",
                "blocker",
                fixable=True,
                category="missing_document",
                doc_types=[t],
            )
        )
    for cond in req.conditional:  # e.g. surgery lines → procedure_bill, implant lines → implant_sticker
        if cond.predicate(ctx) and cond.doc_type not in have:
            findings.append(
                F(
                    "completeness.missing_required",
                    "blocker",
                    True,
                    "missing_document",
                    [cond.doc_type],
                )
            )
    for d in ctx.documents:
        if (
            d.parse_confidence is not None
            and d.parse_confidence < ctx.cfg.gates.min_parse_confidence
        ):
            findings.append(
                F(
                    "completeness.low_parse_confidence",
                    "warning",
                    True,
                    "illegible_document",
                    [d.doc_type],
                    doc=d,
                )
            )
    return outcome_from(findings)
```

### 6.3 Identity (deterministic scoring)
```python
def check_identity(ctx):
    pol, mem = ctx.policy, ctx.member
    if not pol: return failed("identity.policy_not_found", fixable=True, cat="identity_mismatch")
    if not mem: return failed("identity.member_not_found", fixable=True, cat="identity_mismatch")
    name_sim = max(jaro_winkler(norm(ctx.patient.full_name), mem.full_name_norm),
                   token_set_ratio(norm(ctx.patient.full_name), mem.full_name_norm)/100)
    dob_ok = ctx.patient.dob == mem.dob
    gender_ok = ctx.patient.gender == mem.gender
    idh_ok = None if not (ctx.patient.id_proof_hash and mem.id_proof_hash) else ctx.patient.id_proof_hash == mem.id_proof_hash
    score = 0.5*name_sim + 0.3*dob_ok + 0.1*gender_ok + 0.1*(1 if idh_ok in (True,None) else 0)
    findings = []
    if not dob_ok: findings.append(F("identity.dob_mismatch","blocker",fixable=True,...))
    if name_sim < cfg.name_similarity_min: findings.append(F("identity.name_mismatch","blocker" if name_sim<0.7 else "warning",...))
    if idh_ok is False: findings.append(F("identity.id_hash_mismatch","blocker",fixable=False,...))
    return outcome(score, findings, passed=score >= cfg.identity_min_score and not blockers)
```
Weights PROPOSED; tuned by eval harness.

### 6.4 Finalize outcome
```python
def finalize(run) -> Outcome:
    steps = run.steps
    active_blockers = [f for s in steps for f in s.findings if f.severity=="blocker" and not f.overridden]
    fixable = [f for f in active_blockers if f.fixable]
    unfixable = [f for f in active_blockers if not f.fixable]
    if unfixable:
        rec = Recommendation(outcome="reject", reason_codes=[f.code for f in unfixable])   # still human decides
        return to("ready_for_decision", rec)
    if fixable:
        return to("needs_info", queries=group_by_category(fixable))   # → 05 query loop
    calc = latest_calculation(run)
    rec = Recommendation(outcome="approve" if calc.payable==calc.claimed else "partial", amount=calc.payable, ...)
    return to("ready_for_decision", rec)
```
`needs_info` is never raised if a query already open for the same finding codes (dedupe key = hash(sorted codes + doc types)).

### 6.5 Re-verification after query response
Only steps whose inputs changed re-run: new documents → completeness (+ authenticity for new docs + calc if bill changed); corrected identity text → identity. Mapping table `INVALIDATES = {doc_type: [steps]}` in `engine.py`.

### 6.6 Concurrency
Per-case advisory lock `pg_advisory_xact_lock(hashtext(case_id))` around `record_step_result` and `transition`. Run creation unique `(case_id, run_no)`; a second run cannot start while one is `running` (409 `run_in_progress`) unless `force=true` by senior_reviewer.

### 6.7 Authenticity (deterministic part)
```python
def check_authenticity_rules(ctx) -> StepOutcome:
    f = []
    # 1. arithmetic: re-add every document-level bill total if parsed; compare with submission totals
    for doc in ctx.parsed_bills:
        if abs(doc.total - ctx.claimed_gross) > Decimal("1.00"):
            f.append(
                F(
                    "auth.bill_arithmetic",
                    "blocker",
                    fixable=True,
                    cat="billing_discrepancy",
                    doc=doc,
                )
            )
    # 2. date consistency: admission <= procedure date <= discharge; lab/radiology dates within stay ±1 day
    for d in ctx.dated_events:
        if not (ctx.admitted_on - ONE_DAY <= d.date <= ctx.discharged_on + ONE_DAY):
            f.append(
                F("auth.dates_inconsistent", "warning", True, "medical_clarification", doc=d.doc)
            )
    # 3. duplicate claim: same member, same hospital, overlapping stay, status not rejected/closed
    dups = ctx.repos.claims.find_overlapping(
        ctx.member_id, ctx.hospital_id, ctx.admitted_on, ctx.discharged_on, exclude=ctx.case_id
    )
    if dups:
        f.append(
            F("auth.duplicate_claim", "blocker", fixable=False, evidence=[ref(d) for d in dups])
        )
    # 4. same file hash seen on another member's claim
    if ctx.repos.docs.sha_seen_elsewhere([d.sha256 for d in ctx.documents], ctx.member_id):
        f.append(F("auth.duplicate_document", "blocker", False))
    # 5. hospital empanelment
    if ctx.claim_type == "cashless" and ctx.hospital.network_status != "network":
        f.append(F("auth.hospital_not_empanelled", "blocker", False))
    if (
        ctx.hospital.empanelment_valid_till
        and ctx.hospital.empanelment_valid_till < ctx.admitted_on
    ):
        f.append(F("auth.hospital_not_empanelled", "blocker", False))
    # 6. stamp/signature from vision-service results on documents
    for d in ctx.documents_requiring_stamp:
        if not d.vision.stamp_detected:
            f.append(
                F("auth.stamp_missing", "blocker", True, "missing_document", [d.doc_type], doc=d)
            )
        if not d.vision.signature_detected:
            f.append(
                F(
                    "auth.signature_missing",
                    "blocker",
                    True,
                    "missing_document",
                    [d.doc_type],
                    doc=d,
                )
            )
    # 7. tamper: vision tamper score above floor
    for d in ctx.documents:
        if d.vision.tamper_score is not None and d.vision.tamper_score > (
            1 - ctx.cfg.thresholds.authenticity_floor
        ):
            f.append(F("auth.tamper_suspected", "blocker", False, evidence=[ev(d)]))
    return outcome_from(f, score=1 - max([d.vision.tamper_score or 0 for d in ctx.documents] + [0]))
```
The agent (08) adds narrative explanations and cross-document consistency judgements (e.g. diagnosis vs procedure plausibility) as **warnings only**; blockers come from rules above.

### 6.8 Coverage (deterministic part)
```python
def check_coverage(ctx) -> StepOutcome:
    pol, mem, rules = ctx.policy, ctx.member, ctx.cfg.policy_rules
    f = []
    status_ok = pol.status == "active" or (
        pol.status == "lapsed"
        and ctx.admitted_on <= pol.premium_paid_until + timedelta(days=pol.grace_days)
    )
    if pol.status in ("cancelled", "suspended"):
        f.append(F("coverage.policy_inactive", "blocker", False))
    elif not status_ok:
        f.append(F("coverage.policy_inactive", "blocker", False))
    elif pol.status == "lapsed":
        f.append(F("coverage.policy_grace", "warning", False))
    if not (pol.start_date <= ctx.admitted_on <= pol.end_date) or ctx.admitted_on < mem.cover_start:
        f.append(F("coverage.outside_period", "blocker", False))
    days_in_cover = (ctx.admitted_on - mem.cover_start).days
    if days_in_cover < rules.waiting_periods_days.initial and not ctx.is_accident:
        f.append(F("coverage.waiting_period", "blocker", False, detail="initial"))
    for dx in ctx.diagnosis_codes:
        if (
            any(dx.startswith(p) for p in mem.pre_existing)
            and days_in_cover < rules.waiting_periods_days.pre_existing
        ):
            f.append(F("coverage.pre_existing", "blocker", False, detail=dx))
        spec = rules.waiting_periods_days.specific.get(specific_group(dx))
        if spec and days_in_cover < spec:
            f.append(F("coverage.waiting_period", "blocker", False, detail=specific_group(dx)))
        if matches_exclusion(dx, ctx.procedure_codes, rules.exclusions):
            f.append(F("coverage.exclusion", "blocker", False, detail=dx))
    remaining = pol.sum_insured + pol.cumulative_bonus - ctx.utilised
    if remaining <= 0:
        f.append(F("coverage.sum_insured_exhausted", "blocker", False))
    elif remaining < ctx.claimed_amount:
        f.append(
            F("coverage.sum_insured_exhausted", "warning", False, detail=f"remaining {remaining}")
        )
    if ctx.claim_type == "cashless" and ctx.hospital.network_status != "network":
        f.append(F("coverage.not_network", "warning", False))
    return outcome_from(f)
```
`is_accident` derives from ICD-10 chapter S/T/V-Y or the `emergency` flag plus an MLC/FIR document (PROPOSED). Waiting-period checks run against `admitted_on`, not receipt date.

### 6.9 Calculation step
1. Build `CalcInput` from bill lines (after `calc mapper` agent maps lines to rule categories; mapping stored in step `agent_output.line_mapping` and validated: every line mapped exactly once, categories ∈ enum).
2. Call calc-engine `POST /v1/calc` (07 doc) with policy rule version and utilisation snapshot.
3. Persist `calculation_result` (input + output, `engine_version`, `policy_rules_version`).
4. If `output.payable < 0` → finding `calc.negative_payable` (blocker, unfixable) which indicates a rule bug.
5. Unmapped lines (`category=other` with amount > 1% of claim) → `calc.unmapped_line` warning; reviewer sees them highlighted.

### 6.10 Run state machine
```
(create) running ──all steps terminal──► completed ──finalize──► (case transition)
   │  └── step failure(system) ──► failed   (case → ready_for_decision w/ manual verification banner)
   └── superseded by newer run (manual_rerun/query_response) ──► completed(superseded=true)
```
Step state machine: `pending → running → (passed | flagged | failed | skipped)`; terminal states are immutable except via a new run.

### 6.11 Assignment algorithm detail
```python
def pick_assignee(case, reviewers, loads, T_auto):
    pool = [
        r for r in reviewers if r.active and not r.on_leave and required_tags(case) <= r.skill_tags
    ]
    if case.claimed_amount > T_auto:
        pool = [r for r in pool if "senior" in r.roles] or pool
    if not pool:
        return None  # stays unassigned; shown in queue; alert after 15 min
    return min(pool, key=lambda r: (loads.get(r.sub, 0), r.last_assigned_at))
```
Triggered when run starts (initial) and on reassignments. A case is not reassigned while a reviewer is actively viewing it (presence from Redis TTL key).

### 6.12 Sequence: initial verification
```
insurer-api(receipt) → n8n: webhook verification-start {case_id}
n8n → api: POST /internal/cases/{id}/runs {trigger:"initial", client_token}
n8n → api: GET /internal/cases/{id}/context
n8n: wait documents_ready (≤ 10 min) → api: steps/document_fetch/result
n8n → api: steps/completeness/start → (rule result computed by api itself) /result
n8n → crew: identity ∥ authenticity → api: steps/identity|authenticity/result
n8n → crew: coverage → api: result
n8n → crew: calc-mapper → api: calc → api: steps/calculation/result
n8n → api: POST /internal/runs/{id}/finalize → case → ready_for_decision | needs_info; then, for ready_for_decision, n8n calls POST /internal/cases/{id}/decision/auto-decide (04): auto-approve when all hard gates pass and payable ≤ T_auto, otherwise a human task is opened
api → outbox → hospital callback (status)
```
For pure-rule steps (completeness, document_fetch summary) n8n calls `POST /internal/runs/{id}/steps/{step}/evaluate`, an additional endpoint (PROPOSED) where the API computes the result itself and records it; n8n then does not post a body.

## 7. Config / env vars
`INS_ASSIGNMENT_STRATEGY=least_loaded`, `INS_PRESIGN_TTL_SECONDS=900`, `INS_SLA_TICK_SECONDS=300`, `INS_KEYCLOAK_ISSUER`, `INS_KEYCLOAK_AUDIENCE=insurer-api`, `INS_SERVICE_TOKEN_JWKS`. Config domains used: `doc_requirements`, `thresholds`, `policy_rules`, `query_policy`.

## 8. Error handling and edge cases
- Agent result fails Pydantic validation → 422; n8n retries the agent up to 2 times then marks step `failed` with finding `step.agent_invalid_output` (blocker, fixable=False) and routes case to manual review (`ready_for_decision` with banner "manual verification required").
- Agent/deterministic mismatch → keep deterministic; store agent output; add warning `agent.disagrees_with_rules`.
- LLM unavailable: steps depending purely on rules still run; agent-only enrichments (explanations) skipped; case flagged `degraded=true` in step JSON.
- Policy lapsed within grace days: warning `coverage.policy_grace` not blocker; beyond grace: unfixable blocker.
- Admission before `cover_start` for the member: `coverage.outside_period` unfixable.
- Duplicate claim detection (same member, overlapping admission dates, same hospital): `auth.duplicate_claim` blocker unfixable; reviewer can override.
- Reviewer override of an identity blocker requires `senior_reviewer` role.
- Case assigned to deactivated user: reassignment job moves it daily.
- Re-run when hospital has since sent new docs: uses newest non-superseded docs; old run retained.
- Two reviewers open same case: optimistic `updated_at` ETag on mutating reviewer calls (412 on mismatch).

### 8.1 Extra edge cases
| Situation | Behaviour |
|---|---|
| n8n retries `runs` creation after timeout | client_token makes it idempotent; same run returned |
| Result posted for superseded run | 409 `run_superseded`; n8n aborts that branch |
| Identity agent output includes unmasked PII in `message` | server runs `redact()`; if unredactable, reject 422 and log security event |
| Claim has zero documents | completeness `blocker` for every required type; case still goes to `needs_info` |
| Policy has multiple members with same name+DOB | identity picks by `member_id`; if `member_id` absent, ambiguity warning and `needs_info` |
| Reviewer overrides blocker then rerun | override persists by `finding_key`; if evidence changes the key changes and the override no longer applies |
| Recommendation exceeds sum-insured remaining | calc-engine caps; coverage warning exists; outcome `partial` |
| Hospital sends supplement during `running` run | documents saved; run continues; `INVALIDATES` triggers a follow-up partial run after finalize |
| Config published mid-run | run keeps its stamped versions; banner suggests rerun |
| Findings catalogue gains a new code | unknown codes from agents rejected (422) until added; avoids free-form codes |
| Finalize called twice | idempotent; second returns same outcome |
| Two reviewers assign simultaneously | etag conflict → 412 for loser |
| SLA already breached at receipt (clock skew) | priority 1, flag set, audit event |

## 9. Tests
- Unit: each `check_*` function with table-driven cases (≥ 15 per step incl. boundary similarity 0.879/0.880, DOB off by one day, waiting period exactly at boundary).
- Property tests: finalize is monotonic (more blockers never produce `approve`).
- Integration: full run via internal endpoints with canned agent outputs; invalidation mapping; override audit; concurrency (two parallel `result` posts); SLA tick.
- RBAC matrix test: every route × role.
- Workspace endpoint snapshot test (shape stable for UI).
- Golden dataset: 30 synthetic claims with expected finalize outcome (shared with eval harness).

### 9.1 Table-driven unit cases (representative; implement ≥ 15 per step)
| Step | Case | Input highlights | Expected |
|---|---|---|---|
| completeness | all required present | prescription, pharmacy_bill, final_bill (stamped) | `passed`, no findings |
| completeness | implant line without sticker | bill line category `implant` | blocker `missing_required[implant_sticker]` |
| completeness | parse confidence 0.59 vs gate 0.60 | | warning `low_parse_confidence` |
| completeness | parse confidence exactly 0.60 | | no finding |
| completeness | superseded doc ignored | newer doc present | uses newer |
| identity | name sim 0.879 vs min 0.88 | | finding name_mismatch warning |
| identity | name sim 0.880 | | no finding |
| identity | DOB off by one day | | blocker `dob_mismatch` fixable |
| identity | id hash mismatch | both hashes present | blocker unfixable |
| identity | id hash absent in master | | `id_hash_match=None`, score unaffected |
| identity | policy not found | | `failed`, `policy_not_found` fixable |
| authenticity | duplicate overlapping claim | same member/hospital/dates | blocker unfixable |
| authenticity | adjacent but non-overlapping stays | discharge 2 Oct, new admit 3 Oct | no duplicate finding |
| authenticity | bill total off by 1.00 | tolerance 1.00 | passes; 1.01 → blocker |
| authenticity | stamp missing on final bill | vision false | blocker fixable `missing_document` |
| coverage | policy lapsed within grace | | warning `policy_grace` |
| coverage | admission day before cover_start | | blocker `outside_period` |
| coverage | waiting period 29 vs 30 days | initial 30 | 29 → blocker; 30 → none |
| coverage | accident within initial waiting | emergency + MLC | no waiting-period finding |
| coverage | pre-existing I10 prefix within 730 days | | blocker |
| coverage | utilised leaves less than claim | | warning, outcome partial later |
| calculation | negative payable (forced) | | blocker `calc.negative_payable` |
| finalize | only warnings | | `ready_for_decision`, approve/partial |
| finalize | fixable blocker | | `needs_info` |
| finalize | fixable + unfixable | | unfixable wins → reject recommendation |
| finalize | overridden blocker | | excluded from outcome |
| finalize | open query with same dedupe key | | no second query |

### 9.2 Integration scenarios
1. **Clean claim**: canned agent outputs all `passed` → run completes → case `ready_for_decision`, recommendation approve (auto-approved by the decision gate when payable ≤ `T_auto`); outbox has `acknowledged` only (status unchanged for hospital).
2. **Missing document**: completeness blocker → `needs_info`; outbox contains status `under_query`; query creation delegated to 05 (assert hook called with grouped findings).
3. **Agent invalid output**: crew returns schema-breaking JSON twice → step `failed`, banner finding, case `ready_for_decision` with manual verification flag.
4. **Parallel result posts**: identity and authenticity posted simultaneously → advisory lock serialises, both stored, next step computed once.
5. **Rerun after supplement**: new discharge summary → only completeness + authenticity(doc) + calc rerun; identity untouched (assert via step timestamps).
6. **Override**: reviewer overrides warning with comment → audit event, excluded from outcome; senior needed for identity blocker (403 for reviewer).
7. **Degraded mode**: LLM gateway down → rule-only steps run, `degraded=true`, case flagged.
8. **Concurrency**: 20 cases verifying in parallel with 3 workers; no deadlocks (advisory locks keyed per case).

## 10. Acceptance criteria
- [ ] Synthetic claim set produces expected outcomes (≥ 95% agreement; remaining diffs explained).
- [ ] Every step result and override is in the audit chain.
- [ ] Agent disagreement never alters deterministic outputs.
- [ ] `/workspace` p95 < 500 ms for a 60-doc claim.
- [ ] RBAC matrix test green.

## 11. Dependencies
01-insurer-db, 02 (receipt), 07 (calc called in calculation step), 08 (agent schemas), 09 (flow calls these endpoints). Needed by 04, 05, 10.

## 12. Claude Code kickoff prompt
> Read shared-contract docs plus docs/implementation/03-dev-B-insurer/03-api-case-verification-orchestration.md. Plan first. Implement tasks 1-13 in order; write unit tests for deterministic checks before wiring endpoints. Use canned agent outputs; do not depend on insurer-crew running. Report against section 10.
