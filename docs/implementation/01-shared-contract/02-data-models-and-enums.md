# 01-02 — Shared Data Models, Enums and State Machines

Status: PROPOSED. Joint ownership. Code lives at `contract/python/claim_contract/{models,enums,transitions}.py`; JSON Schemas exported to `contract/openapi/schemas/`.

## 1. Goal
Define every object that crosses the hospital↔insurer boundary, the validation rules both sides must enforce identically, and the status state machines. Neither side may redefine these in its own code; both import the `claim_contract` package.

## 2. Inputs / Outputs
- Input: field requirements from the architecture doc (§1, §3) and the claim domain (Indian health insurance cashless/reimbursement).
- Output: a pip-installable Python package, JSON Schemas, test fixtures (valid and invalid), and transition tables used by both APIs and by tpa-sim.

## 3. Conventions (apply to all models)
- Pydantic v2, `model_config = ConfigDict(extra="forbid", strict=True, frozen=False, str_strip_whitespace=True)`.
- Money is `Decimal` with exactly 2 decimal places, serialised as a **string**. No `float` anywhere in the package (a CI grep fails on `float` in `contract/python`).
- Dates `date` as `YYYY-MM-DD`; timestamps `datetime` timezone-aware UTC, serialised RFC 3339 `Z`.
- IDs: UUID (v7) for system objects; business ids as given below.
- Strings: NFC-normalised, max lengths stated; no control characters.
- Enums serialised as lower-snake strings.

## 4. Enums (`enums.py`)

| Enum | Values |
|---|---|
| `ClaimType` | `cashless`, `reimbursement` |
| `AdmissionType` | `planned`, `emergency` |
| `Gender` | `M`, `F`, `O` |
| `DocType` | `prescription`, `procedure_bill`, `discharge_summary`, `final_bill`, `itemised_bill`, `pharmacy_bill`, `lab_report`, `radiology_report`, `investigation_report`, `admission_note`, `preauth_approval`, `claim_form`, `id_proof`, `policy_card`, `cancelled_cheque`, `implant_sticker`, `payment_receipt`, `fir_mlc`, `other` |
| `BillCategory` | `room`, `icu`, `surgery`, `anaesthesia`, `medicine`, `consumable`, `implant`, `investigation`, `consultation`, `other` |
| `HospitalCaseStatus` | `draft`, `docs_pending`, `docs_complete`, `building_claim`, `ready_for_review`, `submitted`, `acknowledged`, `under_query`, `approved`, `partially_approved`, `rejected`, `settled`, `closed` |
| `InsurerCaseStatus` | `received`, `verifying`, `needs_info`, `ready_for_decision`, `awaiting_approval`, `approved`, `partially_approved`, `rejected`, `escalated`, `settled`, `closed` |
| `QueryStatus` | `open`, `draft_ready`, `answered`, `closed`, `escalated` |
| `QueryCategory` | `missing_document`, `illegible_document`, `identity_mismatch`, `medical_clarification`, `billing_discrepancy`, `policy_exclusion`, `other` |
| `DecisionOutcome` | `approve`, `partial`, `reject`, `needs_info` |
| `Severity` | `info`, `warning`, `blocker` |
| `ActorType` | `agent`, `human`, `system`, `external` |
| `SettlementMode` | `NEFT`, `RTGS`, `IMPS`, `CHEQUE` |
| `WithdrawReason` | `patient_requested`, `duplicate`, `hospital_error`, `other` |
| `DocSupplementReason` | `query_response`, `voluntary`, `correction` |

## 5. Core models (`models.py`) — complete field lists

### 5.1 Primitives
```python
class Money(BaseModel):
    amount: Decimal = Field(ge=0, decimal_places=2, max_digits=14)
    currency: Literal["INR"] = "INR"
    # validator: quantize to 0.01, reject NaN/Inf; serializer -> str
```

### 5.2 Patient
| Field | Type | Constraints |
|---|---|---|
| `full_name` | str | 2-120 chars |
| `dob` | date | not future, age ≤ 120 |
| `gender` | Gender | |
| `member_id` | str | 4-32, `^[A-Z0-9\-]+$` |
| `policy_number` | str | 4-40 |
| `id_proof_hash` | str \| None | 64 hex chars; SHA-256 of the raw ID number + hospital salt; raw ID **never** crosses the boundary |
| `contact_masked` | str \| None | e.g. `XXXXXX3210`; optional |

### 5.3 Admission
| Field | Type | Constraints |
|---|---|---|
| `admission_type` | AdmissionType | |
| `admitted_on` | date | ≤ today |
| `discharged_on` | date | ≥ admitted_on, ≤ today |
| `diagnosis_codes` | list[str] | 1-10, ICD-10 regex `^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$` |
| `procedure_codes` | list[str] | 0-10 |
| `treating_doctor` | str | 2-120 |
| `hospital_id` | str | `^HOSP-\d{4}$` |
| `preauth_ref` | str \| None | required when `claim_type=cashless` (simulated pre-auth record exists) |
| `length_of_stay_days` | int | derived = `(discharged_on - admitted_on).days`, must match if sent |

### 5.4 BillLine
| Field | Type | Constraints |
|---|---|---|
| `line_id` | str | unique within claim, `L001`… |
| `code` | str \| None | hospital item code |
| `description` | str | 2-200 |
| `category` | BillCategory | |
| `qty` | Decimal | > 0, 3 dp |
| `unit_price` | Money | |
| `amount` | Money | must equal `qty × unit_price` rounded half-up to 0.01, tolerance ±0.01 |
| `service_date` | date \| None | within admission window |
| `source_doc_id` | UUID | must exist in `documents` |
| `source_page` | int \| None | ≥ 1 |

### 5.5 ClaimTotals
`gross: Money`, `discounts: Money`, `claimed: Money` where `claimed = gross − discounts`; `patient_paid_advance: Money | None`.

### 5.6 DocumentRef
| Field | Type | Constraints |
|---|---|---|
| `doc_id` | UUID | |
| `doc_type` | DocType | |
| `filename` | str | 1-200, no path separators |
| `sha256` | str | 64 hex |
| `size_bytes` | int | 1 – 25,000,000 |
| `mime_type` | str | `application/pdf`, `image/jpeg`, `image/png`, `image/tiff` |
| `download_url` | HttpUrl | presigned, TTL ≤ 24 h |
| `url_expires_at` | datetime | |
| `parse_confidence` | float \| None | 0-1 (not money; allowed float) |
| `pages` | int | 1-300 |
| `received_via` | Literal["upload","scan","email","supplement"] | |

### 5.7 ConfigVersions
`doc_requirements:int`, `deadlines:int`, `router_rules:int`, `confidence_gates:int` (≥1 each). See 01-03.

### 5.8 ClaimSubmission
`contract_version:str` (`^\d+\.\d+$`), `claim_ref:str` (`^HC-\d{4}-\d{6}$`), `claim_type`, `patient`, `admission`, `bill_lines: list[BillLine]` (1-2000), `totals`, `documents: list[DocumentRef]` (1-100), `config_versions`, `journey_id: UUID | None` (v1.1), `submitted_at: datetime`, `hospital_notes: str | None` (≤ 2000).

### 5.9 Acknowledgement
`claim_ref`, `insurer_claim_no` (`^IC-\d{4}-\d{6}$`), `status: InsurerCaseStatus`, `received_at`, `sequence:int`, `document_ingest: {queued:int, failed:int}`.

### 5.10 StatusUpdate
`claim_ref`, `insurer_claim_no`, `status: InsurerCaseStatus`, `hospital_visible_status: HospitalCaseStatus`, `sequence`, `occurred_at`, `note: str|None`, `open_query_ids: list[UUID]`, `decision: Decision | None`.

### 5.11 Query
| Field | Type | Notes |
|---|---|---|
| `query_id` | UUID | |
| `round` | int | 1-3 |
| `category` | QueryCategory | |
| `text` | str | 10-4000 |
| `requested_doc_types` | list[DocType] | may be empty |
| `due_by` | datetime | |
| `status` | QueryStatus | |
| `raised_by` | str | e.g. `agent:query-drafter+human:reviewer-4` |
| `grounding` | list[Citation] | optional: policy clause ids (insurer-side, may be stripped) |

### 5.12 QueryResponse
`query_id`, `answer_text` (≤ 8000), `attached_doc_ids: list[UUID]` (0-20), `responded_by: str`, `responded_at`. Validator: at least one of `answer_text` non-empty or `attached_doc_ids` non-empty.

### 5.13 Decision, Deduction, SettlementNotice
```python
class Deduction(BaseModel):
    line_ref: str
    rule_id: str
    amount: Money
    explanation: str  # explanation <= 500 chars


class Decision(BaseModel):
    outcome: DecisionOutcome
    approved_amount: Money
    deductions: list[Deduction]
    reason_codes: list[str]  # from insurer code list, e.g. ROOM_CAP, EXCL_COSMETIC
    reviewer_ids: list[str]  # 1 or 2 distinct ids (2 required above T_four)
    calc_trace_id: UUID
    policy_version: int
    decided_at: datetime
    supersedes: int | None = None  # v1.1 reserved


class SettlementNotice(BaseModel):
    settlement_id: UUID
    amount: Money
    utr: str
    paid_on: date
    mode: SettlementMode
    tds: Money
```
Validator: `approved_amount ≤ totals.claimed` of the claim (checked server-side by the receiver), `outcome=reject ⇒ approved_amount=0`, `sum(deductions) + approved_amount == claimed` when `outcome in (approve, partial)` (see rules below).

### 5.14 Other
`Citation(source_id, clause, page, snippet≤300)`, `WithdrawRequest(reason: WithdrawReason, note)`, `DocSupplement(reason, query_id|None, documents)`, `DocRefreshRequest/Response` (01-01 §6.2.5), `ProblemDetail` (01-01 §8).

## 6. Example valid fixture (cashless, planned)
Stored as `contract/tests/fixtures/valid/cashless_planned_01.json`; the full JSON appears in 01-01 §6.1.1. Five valid fixtures: cashless-planned, cashless-emergency (no preauth_ref allowed → `preauth_ref` optional for emergency with `intimation_within_hours`), reimbursement-planned, reimbursement-emergency, multi-document-large (1,500 lines).

## 7. Validation rules (cross-field)
| Rule id | Rule | Error code |
|---|---|---|
| V-01 | `sum(bill_lines.amount) == totals.gross` | `totals_mismatch` |
| V-02 | `claimed == gross − discounts` | `totals_mismatch` |
| V-03 | `discharged_on ≥ admitted_on` | `validation_error` |
| V-04 | each `BillLine.source_doc_id` ∈ `documents.doc_id` | `validation_error` |
| V-05 | cashless ⇒ `preauth_ref` present for planned admissions | `validation_error` |
| V-06 | `service_date` within [admitted_on, discharged_on] when present | `validation_error` |
| V-07 | duplicate `line_id` | `validation_error` |
| V-08 | required docs per config version present (hospital-enforced; insurer re-checks its own list) | `validation_error` (hospital) / `needs_info` (insurer) |
| V-09 | `id_proof_hash` format and no raw ID pattern in any string field (Aadhaar `\b\d{4}\s?\d{4}\s?\d{4}\b`, PAN) | `validation_error` |
| V-10 | ICD-10 format; unknown but well-formed code → `warning` annotation, not error | – |
| V-11 | 2 distinct `reviewer_ids` when decision amount > T_four | enforced by insurer |
| V-12 | `reject ⇒ approved_amount == 0` | `validation_error` |

## 8. State machines

### 8.1 Hospital case — transition table (`transitions.py`)
| From | To | Trigger | Guard | Actor |
|---|---|---|---|---|
| draft | docs_pending | first upload | – | desk |
| docs_pending | docs_complete | completeness engine pass | no blocker findings | system |
| docs_complete | docs_pending | new finding / doc removed | – | system |
| docs_complete | building_claim | build requested | – | system/officer |
| building_claim | ready_for_review | claim assembled | builder success | system |
| building_claim | docs_pending | builder found missing data | – | system |
| ready_for_review | submitted | officer sign-off | role=Officer, audit `human.signoff` | officer |
| ready_for_review | docs_pending | officer rejects build | – | officer |
| submitted | acknowledged | ack received | – | system |
| acknowledged | under_query | query callback | – | system |
| under_query | acknowledged | response accepted / status update | – | system |
| acknowledged/under_query | approved \| partially_approved \| rejected | decision callback | – | system |
| approved/partially_approved | settled | settlement callback | – | system |
| rejected | closed | closed by officer or appeal window end | – | officer |
| settled | closed | reconciliation done | – | officer |
| any pre-submit | closed | cancelled | – | officer |
| submitted…under_query | closed | withdraw | insurer not decided | officer |

### 8.2 Insurer case
| From | To | Trigger | Guard |
|---|---|---|---|
| received | verifying | verification flow starts | docs downloaded |
| verifying | needs_info | any blocker finding | query round < 3 |
| needs_info | verifying | response received | – |
| verifying | ready_for_decision | all checks pass or warnings only | calc complete |
| ready_for_decision | approved \| partially_approved | auto-approval (actor=system, audit `decision.auto_approved`) | ALL hard gates pass (identity ≥ `identity_min_score`, authenticity ≥ floor, completeness, calc consistent, no exclusion/waiting-period/high-risk flag) AND payable ≤ `T_auto` |
| ready_for_decision | awaiting_approval | recommendation recorded, human needed | any gate fails or `review_required` flag, OR payable > `T_auto`, OR outcome is reject |
| awaiting_approval | approved \| partially_approved \| rejected | human approver action | T_auto/T_four rules |
| needs_info | escalated | round 3 unanswered/unsatisfied or round-3 `due_by` passed | – |
| escalated | approved \| partially_approved \| rejected | senior human | role=Approver |
| approved/partially_approved | settled | settlement simulated | – |
| settled/rejected | closed | – | – |

Approval rule (user decision, thresholds PROPOSED): all gates pass and payable ≤ `T_auto` → system auto-approves (no human task); `T_auto` < payable ≤ `T_four` → the agent asks one human Approver for the final-stage decision; payable > `T_four` → two distinct Approvers (four-eyes), neither being the Reviewer who prepared it. Any failed gate or flag forces human review even below `T_auto`. Rejections are never automatic: they always go to a human. `T_auto` is re-tuned on the synthetic evaluation set so the false auto-approve rate stays below ~1%.

### 8.3 Query state machine
`open → draft_ready` (insurer-side only; hospital side: `open → draft_ready` after Query Responder drafts) `→ answered → closed`; `open|draft_ready → escalated` on round-3 non-response or unsatisfied answer (the only escalation trigger; no 50%/80% reminders); `answered → open` (next round, new `query_id`, `round+1`).

### 8.4 Cross-side mapping (hospital shows)
| Insurer | Hospital |
|---|---|
| received, verifying, ready_for_decision, awaiting_approval | acknowledged |
| needs_info, escalated | under_query |
| approved / partially_approved / rejected | same |
| settled / closed | settled / closed |

### 8.5 Implementation
```python
TRANSITIONS: dict[HospitalCaseStatus, set[HospitalCaseStatus]] = {...}


def assert_transition(current, target):
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(current, target)
```
Both APIs call `assert_transition` inside the same DB transaction that updates the status and appends the audit event.

## 9. Build tasks
1. `contract/python/claim_contract/enums.py` — all enums from section 4.
2. `models.py` — models per section 5 with validators V-01…V-12 as `@model_validator`.
3. `transitions.py` — tables from 8.1-8.3 as data plus `assert_transition`, `allowed_next`.
4. `scripts/export_schemas.py` → `contract/openapi/schemas/*.json`; CI fails if checked-in schemas are stale.
5. `contract/tests/fixtures/valid/*.json` (5) and `invalid/*.json` (15, one per rule plus malformed cases).
6. `tests/test_models.py` — parametrised over fixtures; expected error codes table.
7. `tests/test_transitions.py` — every allowed edge succeeds, every other pair raises (generated exhaustively from enum product).
8. Hypothesis property tests: random bill lines → totals reconcile; Money round-trip; no float leakage.
9. `ruff` rule/CI grep forbidding `float` for monetary fields.
10. Publish the package into the workspace (`uv` path dependency) and import it from both APIs.

## 10. Test matrix (invalid fixtures)
| Fixture | Violates | Expected |
|---|---|---|
| totals_off_by_paisa | V-01 | `totals_mismatch` |
| discharge_before_admit | V-03 | validation_error |
| unknown_source_doc | V-04 | validation_error |
| cashless_no_preauth | V-05 | validation_error |
| service_date_outside | V-06 | validation_error |
| duplicate_line_id | V-07 | validation_error |
| raw_aadhaar_in_notes | V-09 | validation_error |
| float_money | convention | validation_error (strict) |
| extra_field | extra=forbid | validation_error |
| bad_icd | V-10 | accepted with warning if well-formed; error if malformed |
| negative_amount | Money | validation_error |
| reject_with_amount | V-12 | validation_error |
| single_reviewer_above_t_four | V-11 | insurer rejects approval |
| empty_query_response | QueryResponse validator | validation_error |
| huge_lines (2001) | limit | validation_error |

## 11. Acceptance criteria
- Both APIs import the package; all invalid fixtures rejected with the table's codes; transition tests exhaustive; mypy strict passes; schemas export is reproducible; grep shows no `float` money.

## 12. Claude Code kickoff prompt
> Implement contract/python/claim_contract per docs/implementation/01-shared-contract/02-data-models-and-enums.md, tasks 1-9, with the fixtures and tests in sections 9-10. Do not add business logic. Finish when `uv run pytest contract` and `mypy --strict contract/python` pass.
