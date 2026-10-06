# 02-04 — hospital-api: Completeness Engine and Document-Requirement Config

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/{completeness/,routers/completeness.py,routers/admin_config.py}`.

## 1. Goal
Deterministically decide whether a case has everything required to submit, produce a precise checklist (present / missing / unusable / needs review), raise fixable "needs-info" requests (principle 7), and drive the `docs_pending ⇄ docs_complete` loop. No LLM is involved in the decision.

Design stance:
- **Pure core, impure shell.** `evaluate(ctx, config)` does no I/O; everything it needs is placed in `CaseContext`. The shell (`services/completeness.py`) loads context, calls `evaluate`, persists results, and drives status.
- **Fail toward fixable.** A missing stamp, blurry page or low parse confidence produces a `doc_request` that a desk user can resolve by re-uploading, never a rejection.
- **Deterministic and reproducible.** The same context and config produce byte-identical JSON (sorted items, stable ordering, no timestamps in the result body).
- **Explainable.** Every item cites `rule_id`, and every non-OK status carries machine-readable `reasons[]` plus a human `message`.

### 1.1 Where this engine sits in the pipeline
```
upload → clamav → vision quality → doc-pipeline parse → classify
                                                         │
                          (debounced 5 s)                ▼
                                           completeness.run(case_id)
                                                         │
          ┌──────────────────────────────────────────────┤
          ▼                                              ▼
 completeness_check row                        doc_request rows (needs-info)
          │                                              │
          ▼                                              ▼
 status docs_pending|docs_complete             reminders (n8n flow, doc 08)
          │
          ▼
 SSE `completeness.updated` → UI checklist (doc 10)
```

## 2. Inputs / Outputs
- In: case attributes (claim_type, admission_type, procedure group, flags), documents with `doc_type`, parse results, quality flags, stamps, config `doc_requirements` and `deadlines`.
- Out: `completeness_check` row (immutable per run), `doc_request` rows, status transition, reminder schedule, SSE event.

### 2.1 `CaseContext` fields (everything `evaluate` may read)
| Field | Type | Source |
|---|---|---|
| `case_id` | UUID | `claim_case.id` |
| `claim_type` | `cashless\|reimbursement` | router decision (doc 05) |
| `admission_type` | `planned\|emergency` | router decision |
| `procedure_group` | `str \| None` | derived from `procedure_codes` through `common/procedures.derive_procedure_group` |
| `flags` | `set[str]` | router flags (`implant`, `medico_legal`, `high_value`, `day_care`, `maternity`) |
| `patient` | `PatientFacts` | case/patient rows (name, dob, member_id, policy_number) |
| `admission` | `AdmissionFacts` | admitted_on, discharged_on, diagnosis codes |
| `docs_by_type` | `dict[DocType, list[DocFacts]]` | documents + latest parse + quality |
| `waivers` | `dict[rule_id, Waiver]` | `requirement_waiver` rows |
| `gates` | `ConfidenceGates` | `confidence_gates` config (agreement_min etc.) |
| `now` | datetime | injected for tests (never `datetime.now()` inside `evaluate`) |

`DocFacts`:
```python
@dataclass(frozen=True)
class DocFacts:
    doc_id: UUID
    doc_type: str
    usable_state: Literal[
        "ok", "excluded", "pending_processing"
    ]  # excluded = superseded|infected|deleted|rejected
    uploaded_at: datetime
    quality_flags: frozenset[
        str
    ]  # blurry, cropped, unreadable, black_page, wrong_orientation_unfixable, low_resolution, glare
    quality_score: float  # 0..1 from vision-service
    has_required_stamp: bool | None  # None = not evaluated yet
    stamp_confidence: float | None
    parse_confidence: float | None
    agreement_score: float | None  # two-pass agreement (principle 5)
    classification_confidence: float | None
    typed_json: dict | None  # masked-or-raw parsed fields, hospital-side only
    page_count: int
```

## 3. Data model
`completeness_check.result` JSON:
```json
{"complete":false,"config_version":3,"items":[
 {"rule_id":"R-RX-01","doc_type":"prescription","requirement":"required","status":"present_ok","document_ids":["..."],"severity":"info","message":""},
 {"rule_id":"R-PH-01","doc_type":"pharmacy_bill","requirement":"required","status":"unusable","reasons":["stamp_missing"],"severity":"blocker","message":"Pharmacy bill has no hospital stamp. Please upload the stamped copy."},
 {"rule_id":"R-PROC-01","doc_type":"procedure_bill","requirement":"conditional","condition":"bill has surgery line","status":"missing","severity":"blocker","message":"Procedure bill required for the surgery line"},
 {"rule_id":"R-ORD-01","status":"needs_review","reasons":["not_chronological"],"severity":"warning"}]}
```
Status vocabulary: `present_ok`, `missing`, `unusable` (quality), `needs_review` (low parse/agreement), `waived`, `not_applicable`, `pending_processing`.

### 3.1 `doc_requirements` config payload (PROPOSED schema)
```json
{"rules":[
 {"id":"R-RX-01","doc_type":"prescription","applies":{"claim_type":["*"]},"requirement":"required",
  "min_parse_confidence":0.80,"must_have_fields":["patient_name","date","medicines"]},
 {"id":"R-PH-01","doc_type":"pharmacy_bill","applies":{"claim_type":["*"]},"requirement":"required",
  "must_have_stamp":true,"must_have_fields":["lines","total","date"]},
 {"id":"R-BILL-01","doc_type":"final_bill","applies":{"claim_type":["*"]},"requirement":"required",
  "must_have_stamp":true,"must_have_fields":["lines","total"]},
 {"id":"R-PROC-01","doc_type":"procedure_bill","applies":{"flags":["has_surgery"]},"requirement":"conditional","must_have_stamp":true},
 {"id":"R-IMP-02","doc_type":"implant_sticker","applies":{"procedure_group":["ortho_implant","cardiac_stent"]},"requirement":"conditional"}],
 "alternatives":[{"any_of":["final_bill","itemised_bill"],"for":"R-BILL-01-alt"}],
 "ordering":{"chronological":true,"by":"document_date","rule_id":"R-ORD-01","severity":"warning"},
 "procedure_groups":{"ortho_implant":["0SR9*","0SRB*"],"cardiac_stent":["02703*"]},
 "stamp_rules":{"pharmacy_bill":{"min_stamp_confidence":0.7},"final_bill":{"min_stamp_confidence":0.7},"procedure_bill":{"min_stamp_confidence":0.7},"itemised_bill":{"min_stamp_confidence":0.7}}}
```
**User decision (default seed):** the default required set is prescriptions and bills only (medicine prescriptions with the corresponding medicine bills, procedure bills, hospital-charge bills), arranged chronologically; every bill must carry the hospital stamp. Claim form, ID proof, policy card, discharge summary, pre-auth, cancelled cheque and FIR/MLC rules are no longer seeded; the rule mechanism still supports them as optional/conditional rules added by an Admin (config-driven, no code change). `R-ORD-01` flags documents whose dates are not in chronological order (warning, officer can reorder). `applies` supports `claim_type`, `admission_type`, `procedure_group`, `flags`; `["*"]` means all. Procedure groups map code prefixes (glob) to group names.

### 3.2 DDL (extends doc 01; migration `0014_completeness_extras`)
```sql
CREATE TABLE completeness_check (
  id              uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id         uuid NOT NULL REFERENCES claim_case(id),
  run_no          int  NOT NULL,
  trigger         text NOT NULL CHECK (trigger IN ('doc_event','manual','waive','reclassify','nightly','config_republish','claim_build_precheck')),
  config_version  int  NOT NULL,
  provisional     boolean NOT NULL DEFAULT false,
  complete        boolean NOT NULL,
  blocker_count   int  NOT NULL,
  warning_count   int  NOT NULL,
  result          jsonb NOT NULL,
  result_hash     char(64) NOT NULL,           -- sha256(canonical_json(result)); skip insert if equal to previous
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, run_no)
);
CREATE INDEX ix_completeness_case_latest ON completeness_check (case_id, run_no DESC);

CREATE TABLE requirement_waiver (
  id          uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id     uuid NOT NULL REFERENCES claim_case(id),
  rule_id     text NOT NULL,
  doc_type    text NOT NULL,
  reason      text NOT NULL CHECK (length(reason) >= 10),
  waived_by   uuid NOT NULL REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz,
  UNIQUE (case_id, rule_id) WHERE revoked_at IS NULL
);

-- doc_request already exists in doc 01; columns relied on here:
--   id, case_id, rule_id, doc_type, reason_code, message, status(open|fulfilled|waived|cancelled),
--   due_by, opened_by_run, fulfilled_by_doc_id, created_at, closed_at
CREATE UNIQUE INDEX ux_doc_request_open ON doc_request (case_id, rule_id, reason_code) WHERE status = 'open';
```

### 3.3 Reason codes (closed vocabulary)
| Code | Meaning | Severity | Fixable by |
|---|---|---|---|
| `not_uploaded` | no candidate document of the type | blocker | upload |
| `blurry` / `cropped` / `unreadable` / `black_page` | quality flag from vision-service | blocker | re-scan |
| `wrong_orientation_unfixable` | auto-rotate failed | blocker | re-scan |
| `low_resolution` | below 150 dpi equivalent | warning | optional re-scan |
| `glare` | specular highlights over text | warning | optional |
| `stamp_missing` | required hospital stamp absent | blocker | re-upload stamped copy |
| `stamp_low_confidence` | stamp found but below min | warning | officer review |
| `low_parse_confidence` | parser confidence < rule min | needs_review | officer review |
| `passes_disagree` | two-pass agreement < gate | needs_review | officer review |
| `classification_low_confidence` | classifier unsure of type | needs_review | confirm type |
| `field_missing:<name>` | `must_have_fields` entry absent | blocker for required docs | re-upload / officer edit |
| `patient_name_mismatch` | fuzzy name < threshold | warning (cross-check) | officer review |
| `date_out_of_window` | doc date outside admission ±1 day | warning | officer review |
| `bill_total_mismatch` | lines sum ≠ total | blocker | fix bill |
| `policy_number_mismatch` | card vs declared | warning | officer review |
| `pending_processing` | still being parsed | none (provisional) | wait |

## 4. API / endpoints
```
GET  /v1/cases/{id}/completeness            latest result (+ ?history=true)
POST /v1/cases/{id}/completeness/run        force re-evaluation (desk/officer)
POST /v1/cases/{id}/requirements/{doc_type}/waive    officer {reason}
GET  /v1/cases/{id}/doc-requests            open needs-info items
POST /v1/cases/{id}/doc-requests/{rid}/remind        resend reminder
Admin (admin only):
GET/POST /v1/admin/config/doc_requirements          list versions / create draft
POST /v1/admin/config/doc_requirements/{ver}/validate
POST /v1/admin/config/doc_requirements/{ver}/dry-run   {sample:"last_100_cases"} -> diff of outcomes
POST /v1/admin/config/doc_requirements/{ver}/publish
```
Response of `GET completeness` follows the JSON in §3. Internal trigger: `POST /v1/internal/cases/{id}/completeness/run` (svc-n8n).

### 4.1 Example: `GET /v1/cases/6f1c…/completeness`
```http
HTTP/1.1 200 OK
ETag: "run-7"
Content-Type: application/json

{
  "case_id": "6f1c0d2e-3b7a-7d11-a8c4-5a2b9c3d1e90",
  "run_no": 7,
  "created_at": "2026-10-06T09:14:02Z",
  "provisional": false,
  "complete": false,
  "config_version": 3,
  "summary": {"blockers": 2, "warnings": 1, "needs_review": 1, "ok": 8, "waived": 0},
  "items": [
    {"rule_id":"R-RX-01","doc_type":"prescription","requirement":"required","status":"present_ok",
     "document_ids":["a1…"],"severity":"info","reasons":[],"message":""},
    {"rule_id":"R-BILL-01","doc_type":"final_bill","requirement":"required","status":"unusable",
     "document_ids":["b7…"],"severity":"blocker","reasons":["blurry"],
     "message":"The final bill (page 2) is too blurry to read. Please re-scan page 2 and upload again."},
    {"rule_id":"R-IMP-02","doc_type":"implant_sticker","requirement":"conditional",
     "condition":"procedure_group in [ortho_implant, cardiac_stent]","status":"missing","severity":"blocker",
     "reasons":["not_uploaded"],"document_ids":[],
     "message":"Implant sticker is required for knee replacement (procedure 0SRD0J9)."}
  ],
  "open_doc_requests": [{"id":"d0…","rule_id":"R-BILL-01","due_by":"2026-10-08T09:14:02Z"}]
}
```

### 4.2 Example: `POST /v1/cases/{id}/requirements/procedure_bill/waive`
Request:
```json
{"reason":"Procedure charges are part of the final bill; no separate procedure bill is issued by this hospital"}
```
Response `200`:
```json
{"rule_id":"R-PROC-01","status":"waived","waived_by":"u-officer-12","completeness_run_no":8}
```
Errors: `403 forbidden` for Desk role; `409 not_waivable` for `prescription`/`pharmacy_bill`/`final_bill` unless the rule has `"waivable": true`; `422 reason_too_short` (<10 chars); `404 unknown_rule` when the doc type has no applicable rule on this case.

### 4.3 Example: dry-run
```http
POST /v1/admin/config/doc_requirements/4/dry-run
{"sample":"last_100_cases","compare_to":"published"}
```
```json
{"evaluated":100,"unchanged":87,"changed":13,
 "newly_blocked":[{"case_ref":"HC-2026-000412","rule_id":"R-EMG-01","reason":"not_uploaded"}],
 "newly_cleared":[{"case_ref":"HC-2026-000377","rule_id":"R-RX-01"}],
 "by_rule":{"R-EMG-01":{"newly_blocked":9},"R-RX-01":{"newly_cleared":4}}}
```

## 5. Build tasks
1. Pydantic schemas for `DocRequirementsConfig` and `CompletenessResult`; JSON Schema export used by admin validation. Files: `completeness/schemas.py`, `completeness/schema_export.py`.
2. `completeness/context.py`: `CaseContext` builder (case fields, derived `procedure_group` via glob match, flags like `medico_legal` from router doc 05, documents grouped by type, latest parse per doc).
3. `completeness/rules.py`: pure function `evaluate(ctx, config) -> CompletenessResult` — NO I/O, fully unit-testable.
4. Rule evaluation order per rule: applicability → candidate documents (exclude superseded, infected, deleted) → alternatives (`any_of`) → quality → stamp → confidence → required fields → status.
5. Field presence check: reads `typed_json` of parse; field aliases map (`patient_name`←`patient.name`, …); name matching uses normalised fuzzy compare (rapidfuzz ≥ 90) against case patient; dates compared exactly after normalisation.
6. Cross-document consistency checks (deterministic, produce `warning` items): patient name/dob match across docs; admission/discharge dates consistent between discharge summary and bill; bill total ≥ sum of lines; doc dates within admission window ±1 day; policy number on policy card equals declared.
7. Persistence `services/completeness.py::run(case_id, trigger)`: lock case row `FOR UPDATE`, call `evaluate`, insert `completeness_check` (`run_no` increment), reconcile `doc_request` rows (open new for blockers, mark `fulfilled` when satisfied, never reopen waived), audit `completeness.evaluated`.
8. Status driver: if no blockers and no `needs_review` → `docs_complete` (from `docs_pending`); otherwise `docs_pending`. If `ready_for_review` and a later run finds blockers (doc deleted) → back to `docs_pending` and invalidate draft sign-off.
9. Triggers: after each document `parse/classify/status` callback (debounced 5 s via Redis key), after waive, after manual reclassify, nightly sweep for stale cases.
10. Needs-info message builder: templates per `(doc_type, reason)` in `completeness/messages.py` (en; PROPOSED i18n keys for hi); output is deterministic text, optionally polished later by the crew (doc 09) but never required.
11. Deadlines & reminders: for each open `doc_request` set `due_by` using `deadlines.request_sla_hours`; create `reminder` rows at offsets (`[24h, 48h, 72h]`); n8n reminder flow (doc 08) fires them. For reimbursement, also warn at `filing_deadline - 7d, -2d`.
12. Waive: officer-only; stores reason; item status `waived`; counts as satisfied; audited; blocked for `prescription`/`pharmacy_bill`/`final_bill` unless admin config says waivable (`"waivable": true`).
13. Admin config endpoints reusing `ConfigService` (01-03): draft CRUD, JSON-schema validation, semantic validation (unknown doc types, unreachable rules, overlapping alternatives), dry-run (re-evaluate last N cases with candidate config, return counts of changed outcomes), two-person publish.
14. Metrics: Prometheus counters `completeness_runs_total{result}`, histogram of run duration, gauge of open blockers.
15. Seed config v1 covering cashless/reimbursement × planned/emergency with at least 15 rules.

### 5.1 Step detail: task 7 (persistence shell)
```python
async def run(case_id: UUID, trigger: str, *, db, cfg_svc, audit, events, clock) -> CompletenessRun:
    async with db.begin():
        case = await cases.get_for_update(case_id)  # SELECT ... FOR UPDATE
        cfg_ver = case.config_versions["doc_requirements"] or await cfg_svc.latest_published(
            "doc_requirements"
        )
        if cfg_ver is None:
            raise ConfigUnavailable("doc_requirements")  # 503, fail closed
        cfg = await cfg_svc.load("doc_requirements", cfg_ver)
        ctx = await build_context(db, case, clock.now())
        result = evaluate(ctx, cfg)  # pure
        h = sha256_hex(canonical_json(result.model_dump(mode="json")))
        prev = await completeness_repo.latest(case_id)
        if prev and prev.result_hash == h and trigger != "manual":
            return CompletenessRun(prev, changed=False)  # avoid history spam
        run_no = (prev.run_no + 1) if prev else 1
        row = await completeness_repo.insert(case_id, run_no, trigger, cfg_ver, result, h)
        await reconcile_doc_requests(db, case, result, row)
        new_status = decide_status(case.status, result)
        if new_status != case.status:
            await transitions.apply(case, new_status, actor="system:completeness")
        await audit.append(
            case_id,
            "completeness.evaluated",
            {
                "run_no": run_no,
                "complete": result.complete,
                "blockers": result.blocker_count,
                "config_version": cfg_ver,
            },
        )
    await events.publish(
        "completeness.updated", case_id, {"run_no": run_no, "complete": result.complete}
    )
    return CompletenessRun(row, changed=True)
```

### 5.2 Step detail: `reconcile_doc_requests`
Input: previous open requests and new items. Algorithm:
1. Build desired set `D = {(rule_id, reason_code)}` from items with `severity == "blocker"` and status in (`missing`, `unusable`) — one request per `(rule_id, primary_reason)`.
2. For each open request not in `D`: set `status='fulfilled'`, `fulfilled_by_doc_id` = the document id now satisfying the rule, `closed_at=now()`.
3. For each element of `D` without an open request: insert with `message` from `messages.render(doc_type, reason, ctx)` and `due_by = now + request_sla_hours`.
4. For waived items: close any open request with `status='waived'`.
5. If a request exists with the same `(rule_id, reason_code)` but a new document replaced the bad one and is still bad with the same reason → keep the existing request open, bump `last_seen_run`, do not reset `due_by`.
6. Emit `doc_request.opened` / `doc_request.closed` audit events (counts only).

### 5.3 Step detail: debounce (task 9)
```python
async def schedule_run(case_id, trigger):
    key = f"completeness:debounce:{case_id}"
    # SET NX with 5 s TTL; only the first caller enqueues; later callers extend a "dirty" marker
    if await redis.set(key, "1", nx=True, ex=settings.completeness_debounce_s):
        await redis.set(f"{key}:dirty", "0", ex=60)
        await queue.enqueue_in(settings.completeness_debounce_s, run_job, case_id, trigger)
    else:
        await redis.set(f"{key}:dirty", "1", ex=60)


async def run_job(case_id, trigger):
    await completeness.run(case_id, trigger)
    if await redis.getdel(f"completeness:debounce:{case_id}:dirty") == "1":
        await schedule_run(case_id, "doc_event")  # events arrived during the run
```

## 6. Key logic
```python
def evaluate(ctx, cfg) -> Result:
    items = []
    for rule in cfg.rules:
        if not applies(rule.applies, ctx):
            continue
        cands = [d for d in ctx.docs_by_type.get(rule.doc_type, []) if d.usable_state != "excluded"]
        cands = pick_best(cands)  # latest, highest quality
        if ctx.waived(rule.id):
            items.append(waived(rule))
            continue
        if not cands and not alt_satisfied(rule, ctx):
            items.append(missing(rule))
            continue
        d = cands[0]
        reasons = []
        if d.quality_flags & BLOCKING_FLAGS:
            reasons += list(d.quality_flags & BLOCKING_FLAGS)
        if rule.must_have_stamp and not d.has_required_stamp:
            reasons.append("stamp_missing")
        if d.parse_confidence is not None and d.parse_confidence < rule.min_parse_confidence:
            reasons.append("low_parse_confidence")
        if d.agreement_score is not None and d.agreement_score < cfg_gates.agreement_min:
            reasons.append("passes_disagree")
        missing_fields = [f for f in rule.must_have_fields if not has_field(d, f)]
        status = (
            "unusable"
            if blocking(reasons)
            else "needs_review"
            if reasons or missing_fields
            else "present_ok"
        )
        items.append(item(rule, d, status, reasons, missing_fields))
    items += cross_checks(ctx)
    complete = not any(i.severity == "blocker" for i in items) and not any(
        i.status == "needs_review" for i in items
    )
    return Result(complete, items)
```
`BLOCKING_FLAGS = {blurry, cropped, unreadable, black_page, wrong_orientation_unfixable}`; `stamp_missing` severity is `blocker` for rules with `must_have_stamp` (the "fixable before fatal" case: becomes a doc_request "please re-upload stamped copy").

### 6.1 `pick_best` ordering
Candidates are sorted by the tuple `(usable_state == "ok", -len(blocking_flags), quality_score, uploaded_at)` descending; ties broken by `doc_id` ascending to guarantee determinism. When two documents of the same type are both usable (two bills), `document_ids` lists both, and checks (fields, stamp) must pass on **each** page-bearing document unless the rule sets `"any_candidate": true`.

### 6.2 Applicability matcher
```python
def applies(a: Applies, ctx) -> bool:
    return (
        match(a.claim_type, ctx.claim_type)
        and match(a.admission_type, ctx.admission_type)
        and match(
            a.procedure_group, ctx.procedure_group
        )  # None in ctx never matches a non-wildcard list
        and (not a.flags or bool(set(a.flags) & ctx.flags))
    )  # any-of semantics for flags


def match(allowed, value):
    return allowed is None or "*" in allowed or value in allowed
```

### 6.3 Alternatives
`alternatives[].any_of` lists doc types that satisfy a rule jointly: for rule `R-BILL-01`, either an `itemised_bill` OR a `pharmacy_bill` + `final_bill` pair per config (`"all_of"` is also supported). `alt_satisfied(rule, ctx)` returns true if a satisfying set exists **and** each member passes quality; the result item lists which alternative satisfied the rule in `via`.

### 6.4 Cross-checks catalogue
| ID | Check | Severity | Notes |
|---|---|---|---|
| X-01 | patient name across docs: fuzzy ≥ `HOSP_NAME_MATCH_MIN` | warning | token-sort ratio on normalised names (strip honorifics: Mr/Mrs/Ms/Dr/Smt/Shri) |
| X-02 | DOB equal across docs that carry it | warning→blocker if two docs disagree and both parse confidence ≥ 0.9 | |
| X-03 | admission/discharge dates: summary vs bill vs case | warning | tolerate ±1 day for emergency overnight |
| X-04 | bill total ≥ sum(line amounts) − 0.01 | blocker `bill_total_mismatch` | Decimal |
| X-05 | each doc date within [admitted_on−1, discharged_on+1] | warning `date_out_of_window` | pre-admission investigations exempt by `doc_type in {lab_report, radiology_report}` within 30 days before |
| X-06 | policy number on card equals declared | warning | normalise by stripping spaces/hyphens |
| X-07 | hospital name/registration number consistent across stamped docs | warning | |
| X-08 | doctor signature present on discharge summary | per rule `must_have_fields` | |

## 7. Config / env vars
`HOSP_COMPLETENESS_DEBOUNCE_S=5`, `HOSP_REMINDER_OFFSETS_H=24,48,72` (fallback if config lacks), `HOSP_NAME_MATCH_MIN=90`. Real values come from config tables.

## 8. Error handling and edge cases
- Missing config version → fall back to latest published; none published → 503 `config_unavailable` (fail closed).
- Parse still pending: item `pending_processing` (not blocker) and result flagged `provisional=true`; case stays `docs_pending`.
- Two documents of same type (e.g., two bills): both used; `itemised_bill` totals aggregated by claim builder.
- Reclassification changes outcome: new run supersedes; history retained.
- Concurrent runs: row lock serialises; debounce prevents storms.
- Rule loops/typos in config: validation prevents publish.
- Config changes mid-case: case keeps its snapshot version unless officer triggers "re-evaluate with latest" (records both versions).
- Emergency admissions: pre-auth rule not applicable; PROPOSED rule `R-EMG-01` requires `admission_note` and a post-admission intimation record (`preauth_approval` replaced by `intimation_letter` alternative — add to DocType in contract v1.1 if desired).

### 8.1 Edge-case table (expected behaviour)
| Situation | Behaviour |
|---|---|
| Doc uploaded, ClamAV still scanning | `usable_state=pending_processing`; item `pending_processing`; `provisional=true` |
| Doc flagged infected | `excluded`; if it was the only candidate → `missing` with message "file rejected by virus scan; upload a clean copy" |
| User deletes last candidate after `ready_for_review` | status back to `docs_pending`; sign-off invalidated; audit `signoff.invalidated` |
| Classifier says `other` for all uploads | every required rule `missing`; UI suggests "reclassify" action; classification confidence listed |
| Wrong type uploaded (bill classified as discharge summary) | desk reclassifies → trigger `reclassify` → new run |
| Rule requires `must_have_stamp` but vision-service down | `has_required_stamp=None` → `pending_processing` for that item (not a blocker); a timeout of 10 min converts to `needs_review` with reason `stamp_not_evaluated` |
| Waiver revoked | next run restores original status; open `doc_request` re-opened with new `due_by` |
| Config version published while case in `docs_pending` | case keeps old snapshot; admin "re-evaluate" action available per case or bulk |
| Procedure code missing | `procedure_group=None`; conditional rules for groups are not applicable; item `not_applicable` with `provisional=true` and warning "diagnosis/procedure codes pending" |
| Case has 60 documents | evaluation O(rules × docs); budget < 100 ms |
| Duplicate upload (same sha256) | dedupe at ingest (doc 03); never double-counted here |

## 9. Tests
- Pure unit tests (table-driven, written by Claude Code, no fixed count) for `evaluate` covering every status and rule type, alternatives, conditionals, waivers, flags.
- Property test: adding a satisfying doc never worsens the result.
- Cross-check tests with mismatched names/dates.
- Config validation tests (bad schema, unknown doc type, duplicate ids).
- Dry-run diff test.
- Integration: upload sequence from empty case to `docs_complete`; delete doc after `ready_for_review` returns to `docs_pending`.
- Golden files: 12 synthetic cases with expected checklist JSON in `tests/golden/completeness/`.

### 9.1 Table-driven matrix (excerpt; each row is one pytest param)
| # | Scenario | Claim / adm | Docs present | Expected statuses | `complete` |
|---|---|---|---|---|---|
| T01 | Happy path | cashless/planned | prescription, pharmacy bill, final bill (all clean, stamped, chronological) | all `present_ok` | true |
| T02 | Missing prescription | any | pharmacy bill, final bill | R-RX-01 `missing` | false |
| T03 | Missing pharmacy bill | any | prescription, final bill | R-PH-01 `missing` | false |
| T04 | Surgery without procedure bill | any | has_surgery flag; Rx, pharmacy, final | R-PROC-01 `missing` | false |
| T05 | Blurry bill | any | all, final bill flagged `blurry` | R-BILL-01 `unusable` | false |
| T06 | Pharmacy bill without stamp | any | stamp absent | R-PH-01 `unusable` reason `stamp_missing` | false |
| T07 | Low parse confidence | any | prescription parse conf 0.62 (min .80) | `needs_review` | false |
| T08 | Pass disagreement | any | agreement 0.55 (gate 0.8) | `needs_review` `passes_disagree` | false |
| T09 | Knee implant without sticker | cashless | procedure 0SRD0J9 → ortho_implant | R-IMP-02 `missing` | false |
| T10 | Knee implant with sticker | cashless | + implant_sticker | R-IMP-02 `present_ok` | true |
| T11 | Alt: itemised instead of final bill | any | itemised_bill, no final_bill | R-BILL-01 `present_ok` via alternative if rule config allows | per config |
| T12 | Waived procedure bill | any | procedure bill waived | `waived`, counts as satisfied | true |
| T13 | Bills out of chronological order | any | pharmacy bill dated before prescription | R-ORD-01 `needs_review` (warning) | true |
| T14 | Two bills, one blurry | any | bill A ok, bill B blurry | R-BILL-01 `unusable` (each must pass) | false |
| T15 | Pending parse | any | prescription parse pending | `pending_processing`, `provisional=true` | false |
| T16 | Name mismatch across docs | any | prescription "Rahul Sharma", bill "Rakesh Sharma" | X-01 warning | true (warning only) |
| T17 | Bill total mismatch | any | lines sum 1,84,500 vs total 1,80,000 | X-04 blocker | false |
| T18 | Excluded (infected) only candidate | any | only prescription infected | `missing` | false |
| T19 | Superseded doc ignored | any | old blurry bill superseded by clean one | `present_ok` | true |
| T20 | Date out of window | any | prescription 6 months old | X-05 warning (exempt window exceeded) | true |

### 9.2 Property and determinism tests
- **Monotonicity**: for any context `c` and any `doc` that satisfies a missing rule, `score(evaluate(c+doc)) >= score(evaluate(c))` where score counts blockers negatively.
- **Idempotence**: `evaluate(c, cfg)` called twice returns `==` JSON bytes.
- **Order independence**: shuffling `ctx.docs_by_type` lists and `cfg.rules` produces the same sorted `items` output (output is sorted by `(rule_id, doc_type)`).
- **Config round-trip**: every seed config validates against exported JSON Schema.

### 9.3 Golden files
`tests/golden/completeness/{G01..G12}.input.json` + `.expected.json`. Regenerate with `make golden-update` (requires review diff in PR). Scenarios: planned cashless knee replacement, emergency cashless cardiac stent, day-care cataract, maternity reimbursement, MLC road-traffic injury, high-value oncology, ICU stay, missing stamp, blurry bill, name mismatch, pending parse, waived cheque.

## 10. Acceptance criteria
- [ ] 100% branch coverage on `rules.py`.
- [ ] Golden cases pass; same input always yields byte-identical result JSON.
- [ ] A missing stamp yields a doc_request, not a rejection.
- [ ] Publishing needs two distinct admins; dry-run shows impact.
- [ ] Evaluation of a 40-document case < 100 ms (excluding DB).

## 11. Dependencies
Doc 01, 03 (documents/parse callbacks), 05 (flags, claim type), vision-service quality outputs, config service (01-03). Used by doc 06, 08, 10.

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/04-api-completeness-engine.md. Begin with the pure `evaluate` function and its table-driven tests (tasks 1-6), then persistence/status driver (7-9), then reminders, waive and admin endpoints (10-13).
