# 03-07 — calc-engine: Deterministic Payout Calculation (2.3)

Owner: Dev B. Status: PROPOSED. Container `calc-engine` (port 8120) wrapping a pure Python package `insurer/calc_engine/`.
Depends on: policy rules config (01-03), contract models (01-02), insurer policy tables (via the API only, see §4). Consumed by 03 (calculation step) and 08 (Calculation mapper output is the engine's input).

## 1. Goal
Compute the payable amount for a claim from structured inputs using only code: whole-claim blocks, exclusions, sub-limits, room-rent proportionate deduction, per-line caps, deductible, co-pay, sum-insured cap — in one fixed, documented order, with a line-by-line trace that a human reviewer can read. No LLM touches arithmetic (principle 1). The engine is pure: no I/O, no clock, no randomness, no DB. Same input → byte-identical output. 100% branch coverage.

Non-goals: deciding approve/reject (decision gate, doc 04), mapping free-text lines to groups (doc 08 + `mapping.py` fallback), fraud/authenticity (doc 08), payments (doc 06).

## 2. Inputs / Outputs

### 2.1 Input `CalcInput` (pydantic v2, `frozen=True`, `extra="forbid"`, all money `Decimal`, floats rejected)
```python
class PolicySnapshot(BaseModel):
    policy_number: str
    product_code: str
    status: Literal["active", "lapsed", "cancelled", "suspended"]
    start_date: date
    end_date: date
    premium_paid_until: date
    grace_days: int = 30
    sum_insured: Decimal
    bonus_sum: Decimal = 0  # cumulative bonus already added
    utilised_this_year: Decimal = 0  # API aggregates family for floater
    sum_insured_basis: Literal["individual", "floater"]


class MemberSnapshot(BaseModel):
    member_id: str
    relationship: Literal["self", "spouse", "child", "parent", "other"]
    dob: date
    cover_start: date  # continuous cover start (portability-adjusted by API)
    pre_existing: list[PreExisting]  # [{icd_prefix, declared_on}]


class HospitalFacts(BaseModel):
    hospital_id: str
    network_status: Literal["network", "non_network"]
    room_rent_tier: Literal["A", "B", "C"] | None


class AdmissionFacts(BaseModel):
    admitted_on: date
    discharged_on: date
    admission_type: AdmissionType
    diagnosis_codes: list[str]
    procedure_codes: list[str]
    procedure_group: str | None  # primary group, e.g. "knee_replacement"
    day_care: bool = False
    hospital: HospitalFacts


class CalcLine(BaseModel):
    line_ref: str  # stable, unique per case, e.g. "L0007"
    category: str  # contract BillLine.category
    mapped_group: MappedGroup
    procedure_group: str | None  # which sub-limit / waiting group the line belongs to
    description: str
    qty: Decimal
    unit_price: Decimal
    claimed_amount: Decimal
    service_date: date | None  # needed for pre/post-hospitalisation windows
    room_type: Literal["ward", "semi_private", "single", "deluxe", "icu"] | None
    days: int | None  # room/ICU lines only
    is_non_medical: bool = False
    is_implant: bool = False
    exclusion_tags: list[str] = []
    mapping_source: Literal["rule", "agent"]  # provenance from doc 08
    source_doc_id: UUID
    source_page: int | None


class CalcInput(BaseModel):
    case_id: UUID
    claim_type: ClaimType
    admission_type: AdmissionType
    policy: PolicySnapshot
    member: MemberSnapshot
    admission: AdmissionFacts
    lines: list[CalcLine]  # 1..CALC_ENGINE_MAX_LINES
    rules: PolicyRules
    rules_version: int  # resolved by API from config (01-03)
    advance_paid: Decimal = 0  # informational, never subtracted by engine
```
`MappedGroup` enum: `room_rent, icu, nursing, doctor_fees, surgeon_fees, anaesthesia, ot_charges, implant, medicine, consumable, investigation, procedure_package, ambulance, pre_hospitalisation, post_hospitalisation, non_medical, other`.

### 2.2 Output `CalcResult`
```python
class LineResult(BaseModel):
    line_ref: str
    claimed: Decimal
    disallowed: Decimal  # total removed by exclusions/caps/proportionate (S2-S5)
    allowed_after_caps: Decimal  # claimed - disallowed
    deductible_share: Decimal  # S7 allocation
    co_pay_share: Decimal  # S8 allocation
    sum_insured_cut: Decimal  # S10 allocation
    payable: Decimal
    rule_trace: list[LineRuleHit]  # [{rule_id, step, amount, explanation}]


class CalcResult(BaseModel):
    engine_version: str
    rules_version: int
    claimed_total: Decimal
    eligible_total: Decimal  # Σ allowed_after_caps  (end of S6)
    payable_total: Decimal
    patient_pays_total: Decimal  # claimed_total - payable_total
    lines: list[LineResult]
    summary_deductions: list[
        Deduction
    ]  # contract Deduction(line_ref, rule_id, amount, explanation)
    flags: list[CalcFlag]
    trace: list[
        TraceStep
    ]  # {step, rule_id, description, before_total, after_total, affected_lines}
    blocked: BlockedReason | None
    remaining_sum_insured_before: Decimal
    remaining_sum_insured_after: Decimal
```
`BlockedReason`: `POLICY_INACTIVE, PREMIUM_LAPSED, OUTSIDE_COVER, INITIAL_WAITING, PRE_EXISTING_WAITING, SPECIFIC_WAITING, SUM_INSURED_EXHAUSTED, ALL_LINES_EXCLUDED`.
`CalcFlag` (code + message + optional `line_ref`): `SUM_INSURED_CAPPED, WAITING_PERIOD_HIT, UNMAPPED_LINE, NO_DATE_ON_LINE, TOTAL_MISMATCH, PREMIUM_IN_GRACE, DEGRADED_MAPPING, ROOM_DAYS_ASSUMED, CO_PAY_SELECTED, ORDER_PROFILE_NON_DEFAULT`.

### 2.3 Invariants (checked at S12; violation → `EngineInvariantError`, API returns 500 with trace)
1. `0 ≤ payable_total ≤ claimed_total`.
2. `Σ line.payable == payable_total` exactly (cents).
3. `payable_total ≤ remaining_sum_insured_before`.
4. For every line: `claimed == payable + disallowed + deductible_share + co_pay_share + sum_insured_cut`.
5. Every disallowed rupee appears in exactly one `Deduction` with a known `rule_id`.
6. `Σ summary_deductions.amount == patient_pays_total`.
7. All Decimals have exponent ≥ −2 (cent resolution).

## 3. Data model

### 3.1 Policy rules JSON (`config.policy_rules`, validated by `PolicyRules`; schema_version 1)
```json
{
 "schema_version": 1,
 "order_profile": "standard",
 "room_rent": {"type":"percent_of_si","percent":"1.0","icu_percent":"2.0","per_day_cap":null,
               "tier_caps":{"A":"7000","B":"5000","C":"4000"}, "use_tier_caps": false},
 "proportionate_deduction": true,
 "proportionate_applies_to": ["doctor_fees","surgeon_fees","anaesthesia","ot_charges","nursing","investigation","procedure_package"],
 "proportionate_exempt": ["medicine","implant","consumable"],
 "sub_limits": {"cataract":"40000","knee_replacement":"150000","maternity":"75000"},
 "line_caps": {"ambulance":"3000"},
 "hospitalisation_windows": {"pre_days":30,"post_days":60},
 "co_pay": {"percent":"10","conditions":{"age_gte":61}},
 "non_network_co_pay_percent":"20",
 "stack_co_pay": false,
 "co_pay_order": "after_deductible",
 "deductible": {"amount":"0","type":"per_claim"},
 "waiting_periods_days": {"initial":30,"pre_existing":730,"specific":{"cataract":730,"hernia":365}},
 "pre_existing_group_map": {"E11":["diabetes_management"],"I10":["hypertension_management"]},
 "accident_icd_prefixes": ["S","T"],
 "exclusions": {"icd_prefixes":["Z41"],"tags":["cosmetic","dental_cosmetic","non_medical"],"non_medical_policy":"exclude_all"},
 "day_care_groups": ["cataract"],
 "sum_insured_basis": "floater"
}
```
Defaults: every key above has a documented default in `rules_schema.py`; a *missing required key* (`room_rent`, `waiting_periods_days`, `exclusions`) is a validation error, never silently defaulted (API boundary → 422 `rules_invalid`).

### 3.2 Rule-id catalogue (stable strings; used in traces, Deductions, UI, audit)
| Rule id | Step | Meaning |
|---|---|---|
| R-BLK-01 | S0 | Policy not active |
| R-BLK-02 | S0 | Premium lapsed beyond grace |
| R-BLK-03 | S0 | Admission outside policy period / before member cover start |
| R-WAIT-01 | S1 | Initial waiting period (non-accident) |
| R-WAIT-02 | S1 | Pre-existing disease waiting (group blocked) |
| R-WAIT-03 | S1 | Specific-illness waiting (group blocked) |
| R-EXCL-01 | S2 | Excluded diagnosis (ICD prefix) |
| R-EXCL-02 | S2 | Excluded tag (cosmetic, dental_cosmetic …) |
| R-EXCL-03 | S2 | Non-medical item |
| R-SUB-01 | S3 | Procedure-group sub-limit scaling |
| R-ROOM-01 | S4 | Room rent above eligible rate (room line cut) |
| R-ROOM-02 | S4 | ICU rent above eligible rate (ICU line cut) |
| R-PROP-01 | S4 | Proportionate deduction on dependent group |
| R-CAP-01 | S5 | Ambulance cap |
| R-CAP-02 | S5 | Outside pre-hospitalisation window |
| R-CAP-03 | S5 | Outside post-hospitalisation window |
| R-DED-01 | S7 | Per-claim deductible |
| R-COPAY-01 | S8 | Co-pay (age condition) |
| R-COPAY-02 | S8 | Co-pay (non-network) |
| R-COPAY-03 | S8 | Co-pay (stacked) |
| R-SI-01 | S10 | Sum insured cap |
| R-SI-02 | S10 | Sum insured exhausted |

## 4. API / endpoints (thin FastAPI wrapper; the package is the product)
| Method | Path | Notes |
|---|---|---|
| POST | `/v1/calculate` | body `CalcInput` → `CalcResult`; pure; stateless; no DB; p95 < 50 ms for 200 lines |
| POST | `/v1/calculate/batch` | `{inputs:[CalcInput]}` (max 500) → `{results:[CalcResult|ProblemDetail]}`; for eval/replay |
| POST | `/v1/calculate/explain` | same body → `text/plain` human trace (same output as the CLI) |
| GET | `/v1/version` | `{engine_version, rules_schema_versions:[1], order_profiles:["standard","room_first"]}` |
| GET | `/v1/health` | liveness |
Auth: service JWT (`svc-insurer-api`, `svc-eval`), checked by a FastAPI dependency; network-internal only.

PROPOSED (D-0): the original plan lists insurer-db access for policy params. The engine stays DB-free; insurer-api loads the policy snapshot and resolves rules, then passes both. This keeps it testable, replayable from audit data, and prevents hidden state.

Errors (RFC 7807, contract 01-01 style): `422 validation_error` (floats, negatives, bad enum), `422 rules_invalid`, `413 too_many_lines`, `500 engine_invariant_violated` (body carries trace), `500 internal_error`.

Example request (abridged):
```json
{"case_id":"0191f0a2-…","claim_type":"cashless","admission_type":"planned",
 "policy":{"policy_number":"P-1","product_code":"HF-GOLD","status":"active","start_date":"2026-01-01","end_date":"2026-12-31",
           "premium_paid_until":"2026-12-31","sum_insured":"500000.00","bonus_sum":"0.00","utilised_this_year":"0.00","sum_insured_basis":"individual"},
 "member":{"member_id":"M-9","relationship":"self","dob":"1981-04-02","cover_start":"2024-01-01","pre_existing":[]},
 "admission":{"admitted_on":"2026-09-01","discharged_on":"2026-09-04","admission_type":"planned","diagnosis_codes":["K35.8"],
              "procedure_codes":[],"procedure_group":null,"hospital":{"hospital_id":"H-1","network_status":"network","room_rent_tier":null}},
 "lines":[{"line_ref":"L0001","category":"room","mapped_group":"room_rent","room_type":"semi_private","days":3,"qty":"3","unit_price":"4500.00",
           "claimed_amount":"13500.00","mapping_source":"rule","source_doc_id":"…","description":"Semi-private room"}],
 "rules":{"schema_version":1,"…":"…"},"rules_version":12}
```
Example response (abridged, Ex-1):
```json
{"engine_version":"1.0.0+3fa9c1e","rules_version":12,"claimed_total":"98000.00","eligible_total":"98000.00",
 "payable_total":"98000.00","patient_pays_total":"0.00","blocked":null,"flags":[],
 "lines":[{"line_ref":"L0001","claimed":"13500.00","disallowed":"0.00","payable":"13500.00","rule_trace":[]}],
 "summary_deductions":[],"trace":[{"step":"S6","rule_id":null,"description":"Eligible subtotal","before_total":"98000.00","after_total":"98000.00"}],
 "remaining_sum_insured_before":"500000.00","remaining_sum_insured_after":"402000.00"}
```

## 5. Build tasks
1. Package layout `insurer/calc_engine/`:
```
calc_engine/
  __init__.py  __main__.py (CLI: explain, run, golden-update)
  models.py            # CalcInput/CalcResult/enums (this doc §2)
  rules_schema.py      # PolicyRules + defaults + validators
  money.py             # q(), pct(), ratio(), allocate_cents()
  state.py             # EngineState, LineState (mutable working copy; never exposed)
  trace.py             # TraceRecorder
  errors.py            # EngineInvariantError, RulesInvalidError
  engine.py            # orchestrator: choose order_profile, run steps, invariants
  steps/ s00_prechecks.py s01_waiting.py s02_exclusions.py s03_sublimit.py s04_room.py
         s05_caps.py s06_eligible.py s07_deductible.py s08_copay.py s10_sum_insured.py s11_allocate.py s12_invariants.py
  mapping.py           # deterministic category→group + keyword rules (shared with doc 08)
  api.py  Dockerfile
tests/ unit/ golden/ property/ reference_calc.py api/
```
2. `money.py` first (task order matters, everything depends on it): see §6.1. Unit-test with the rounding tables in §9.
3. `models.py` + `rules_schema.py`: pydantic strict; custom validator rejects `float`; `Decimal` quantised at validation to 2dp (inputs with > 2dp are 422, not silently rounded).
4. `state.py`: `LineState{line_ref, claimed, allowed, hits[], flags}`; `EngineState{lines, blocked, flags, trace, deductible_total, co_pay_total, si_cut_total}`.
5. Implement steps S0 → S12 as pure functions `step(state, inp) -> state` (return new state; no in-place mutation outside `state.py` helpers). Each step records exactly one `TraceStep`.
6. `engine.py`: `run(inp) -> CalcResult`; selects `order_profile` ("standard": S3→S4, "room_first": S4→S3; D-3), and `co_pay_order` ("after_deductible" default, "before_deductible"). Non-default profile adds flag `ORDER_PROFILE_NON_DEFAULT`.
7. `mapping.py`: `category→mapped_group` table + keyword rules YAML; engine treats unmapped lines as `other`, flag `UNMAPPED_LINE`, and `needs_review` (reviewer must look). Engine never maps by LLM.
8. `s12_invariants.py` (§2.3).
9. FastAPI wrapper, Dockerfile (python:3.12-slim, non-root, no network egress required), compose entry (profile `insurer`), healthcheck.
10. Worked-example tests (§9.1) **before** step implementations; they are the specification. Parametrised from `tests/golden/*.json`.
11. Property tests (§9.3) and independent reference implementation `tests/reference_calc.py` (§9.4; written by Dev B in a separate sitting, no code sharing).
12. CLI `python -m calc_engine explain tests/golden/ex02.json` prints the trace in §9.5 format.
13. Versioning: `engine_version = "<semver>+<git sha>"`; arithmetic-affecting change ⇒ semver bump + regenerated goldens with a reviewer note in the PR; non-arithmetic ⇒ patch.
14. CI: 100% branch coverage, mypy strict on package, mutmut ≥ 90% on `steps/` and `money.py`, differential test, ruff.

## 6. Key logic

### 6.1 Money and rounding rules (binding)
1. All amounts are `Decimal`; `decimal.getcontext().prec = 28`; **no float anywhere** (ruff rule bans `float(` and `: float` in the package).
2. Intermediate ratios (`eligible_rate/actual_rate`, `L/sum_g`) are kept as full-precision `Decimal` and **never rounded**. Rounding to cents happens only when a total is split among lines (allocation) or a single percentage amount is produced.
3. `q(x) = x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`. Used for single-amount results (e.g. co-pay total).
4. Splitting a total over lines uses **largest-remainder allocation** so the cents always sum exactly:
```python
def allocate_cents(total: Decimal, exact: dict[str, Decimal]) -> dict[str, Decimal]:
    """exact = unrounded per-line shares whose sum is ≈ total. Returns cent amounts summing to q(total)."""
    target = int((q(total) * 100))  # total in cents
    floors = {k: int((v * 100).to_integral_value(ROUND_FLOOR)) for k, v in exact.items()}
    short = target - sum(floors.values())  # 0 <= short < len(exact)
    rema = sorted(
        exact, key=lambda k: (-((exact[k] * 100) - floors[k]), k)
    )  # biggest fractional remainder first; tie → line_ref asc
    for k in rema[:short]:
        floors[k] += 1
    return {k: Decimal(c) / 100 for k, c in floors.items()}
```
Guards: `short < 0` or `short > len` ⇒ `EngineInvariantError("allocation_drift")`.
5. A line's `allowed` can never go negative or exceed `claimed` (clamped, with the clamp recorded as trace note; a clamp indicates a bug and fails property tests).
6. Percent inputs are strings in config (`"10"`), parsed as `Decimal`; percent amount = `total × percent / 100`, then `q()`.
7. Zero-value lines are carried through with all-zero results and flag (`qty=0`) but never divide.

### 6.2 Orchestrator
```python
def run(inp: CalcInput) -> CalcResult:
    st = EngineState.from_input(inp)
    st = s00_prechecks(st, inp)
    if not st.blocked:
        st = s01_waiting(st, inp)  # may block whole claim or specific groups
    if not st.blocked:
        st = s02_exclusions(st, inp)
        order = (
            [s03_sublimit, s04_room]
            if inp.rules.order_profile == "standard"
            else [s04_room, s03_sublimit]
        )
        for step in order:
            st = step(st, inp)
        st = s05_caps(st, inp)
        st = s06_eligible(st, inp)
        if inp.rules.co_pay_order == "after_deductible":
            st = s07_deductible(st, inp)
            st = s08_copay(st, inp)
        else:
            st = s08_copay(st, inp)
            st = s07_deductible(st, inp)
        st = s10_sum_insured(st, inp)
    st = s11_allocate(st, inp)
    s12_invariants(st, inp)
    return st.to_result(inp)
```
Whole-claim block ⇒ every line gets `disallowed = claimed` with the block rule id; later steps skipped; trace shows `S0`/`S1` and a final `S12`.

### 6.3 Order of operations (fixed; PROPOSED; must be signed off in the PR by both devs)
```
S0  Pre-checks (whole-claim blocks)       policy status, premium, period, cover start
S1  Waiting periods & pre-existing        initial; pre-existing; specific — whole claim or group blocks
S2  Exclusions                            ICD prefixes, tags, non-medical → disallow 100% of those lines
S3  Procedure sub-limit                   scale the group's lines to the limit
S4  Room rent eligibility + proportionate deduction
S5  Per-line caps and hospitalisation windows
S6  Eligible subtotal
S7  Deductible (per claim)
S8  Co-pay (percentage on the remaining amount)
S10 Sum-insured cap                       min(payable, remaining SI)
S11 Allocation to lines (largest remainder)
S12 Invariants
```
(S9 "network adjustments" from the first draft is folded into S8 conditions; the number is kept free so existing references stay valid.)
Decisions: **D-1** exclusions precede caps so excluded amounts never consume a sub-limit. **D-2** deductible precedes co-pay (co-pay is on what the insurer would otherwise pay). **D-3** sub-limit precedes room proportionate deduction (stricter for the claimant; `room_first` profile available, divergence in Ex-15). Where real policy wording differs, change `order_profile` / `co_pay_order` in rules, not code.

### 6.4 Step logic with pseudocode

**S0 Pre-checks.**
```python
if p.status != "active":
    block("POLICY_INACTIVE", "R-BLK-01")
elif p.premium_paid_until < admitted_on:
    if (admitted_on - p.premium_paid_until).days > p.grace_days:
        block("PREMIUM_LAPSED", "R-BLK-02")
    else:
        flag("PREMIUM_IN_GRACE")
if not (p.start_date <= admitted_on <= p.end_date) or admitted_on < m.cover_start:
    block("OUTSIDE_COVER", "R-BLK-03")
```
Discharge after `end_date` is allowed (admission date governs).

**S1 Waiting periods.** `days_cover = (admitted_on - m.cover_start).days`.
```python
accident = admission_type == emergency and any(
    code.startswith(pfx) for code in dx for pfx in rules.accident_icd_prefixes
)
if days_cover < initial and not accident:
    block("INITIAL_WAITING", "R-WAIT-01")  # whole claim
for pe in m.pre_existing:  # pe.icd_prefix e.g. "E11"
    if any(code.startswith(pe.icd_prefix) for code in dx) and days_cover < waiting.pre_existing:
        groups = rules.pre_existing_group_map.get(pe.icd_prefix)
        if groups:
            block_groups(groups, "R-WAIT-02")  # lines with procedure_group in groups
        else:
            block("PRE_EXISTING_WAITING", "R-WAIT-02")  # group unknown ⇒ whole claim (conservative)
for g, d in waiting.specific.items():
    if admission.procedure_group == g and days_cover < d:
        block_groups([g], "R-WAIT-03")  # SPECIFIC_WAITING
```
Group blocks disallow 100% of those lines; flag `WAITING_PERIOD_HIT` with the group. If all lines end up disallowed ⇒ `blocked = ALL_LINES_EXCLUDED` at S6 (payable 0).

**S2 Exclusions.** A line is excluded when any of:
- primary diagnosis code startswith an `exclusions.icd_prefixes` entry (R-EXCL-01) — applies to lines whose `procedure_group` is the admission primary group, or all lines when the group is null;
- `set(line.exclusion_tags) ∩ rules.exclusions.tags` non-empty (R-EXCL-02);
- `line.is_non_medical` and `non_medical_policy == "exclude_all"` (R-EXCL-03). Policy `"exclude_listed"` excludes only lines tagged `non_medical`; `"allow"` skips.
`allowed = 0`, `disallowed = claimed`, hit recorded.

**S3 Sub-limit.** Group `g` has limit `L = rules.sub_limits[g]`:
```python
members = [l for l in lines if l.procedure_group == g and l.allowed > 0]
sum_g = Σ l.allowed
if sum_g > L and sum_g > 0:
    exact = {l.line_ref: l.allowed * L / sum_g for l in members}      # unrounded
    new = allocate_cents(L, exact)
    for l in members: record_cut(l, l.allowed - new[l.line_ref], "R-SUB-01"); l.allowed = new[l.line_ref]
```
Lines whose group has no sub-limit are untouched. Excluded lines are not in `members` (D-1).

**S4 Room rent and proportionate deduction.**
```python
def eligible_rate(kind):  # kind in {"room","icu"}
    pct = rules.room_rent.percent if kind == "room" else rules.room_rent.icu_percent
    by_si = policy.sum_insured * pct / 100
    caps = [by_si]
    if rr.per_day_cap: caps.append(rr.per_day_cap)
    if rr.use_tier_caps and hospital.room_rent_tier: caps.append(rr.tier_caps[hospital.room_rent_tier])
    return min(caps)
for l in room_and_icu_lines:
    rate = l.unit_price                           # per-day price on the line
    cap = eligible_rate("icu" if l.mapped_group == "icu" else "room")
    days = l.days or max(1, (discharged_on - admitted_on).days)      # flag ROOM_DAYS_ASSUMED if l.days missing
    if rate > cap:
        l.allowed = min(l.allowed, cap * days); ratio[kind] = min(ratio.get(kind, 1), cap / rate)
        record_cut(l, ..., "R-ROOM-01"|"R-ROOM-02")
if rules.proportionate_deduction and ratio:
    applied = min(ratio.values())                  # D-4: one ratio, the smallest, so ICU+room stays consistent
    for l in lines where mapped_group in proportionate_applies_to and not in proportionate_exempt:
        new = l.allowed * applied  → allocate per group with allocate_cents; record "R-PROP-01"
```
Day-care admissions (`admission.day_care` or `procedure_group in day_care_groups`) have no room lines to evaluate; S4 is a no-op for them and the package line is untouched (package is a `procedure_package` line that is subject to proportionate deduction only when a room line exists).
Rate comparison uses the *per-day price on the line*, not the average over a mixed stay: multiple room lines are each compared with the cap; the ratio is the minimum.

**S5 Per-line caps and windows.**
```python
ambulance: Σ allowed over mapped_group=="ambulance" capped to line_caps.ambulance (allocate_cents)           # R-CAP-01
for l in pre_hospitalisation lines:
    if l.service_date is None: flag("NO_DATE_ON_LINE", l)         # allowed, needs review
    elif (admitted_on - l.service_date).days > windows.pre_days: disallow(l, "R-CAP-02")
for l in post_hospitalisation lines: ... (service_date - discharged_on).days > windows.post_days → "R-CAP-03"
```
**S6 Eligible subtotal.** `eligible = Σ allowed`. If `eligible == 0` and not already blocked ⇒ `blocked = ALL_LINES_EXCLUDED`, skip S7–S10.

**S7 Deductible.** `ded = min(rules.deductible.amount, eligible)`; `after_ded = eligible − ded`; allocate `ded` over lines proportionally to `allowed` (largest remainder). Rule R-DED-01. Type `per_claim` only in v1 (`per_year` needs prior-claim history: out of scope, flag `DEDUCTIBLE_TYPE_UNSUPPORTED` → 422 at rules validation).

**S8 Co-pay.**
```python
cands = []
if member_age_on(admitted_on) >= rules.co_pay.conditions.age_gte: cands.append((rules.co_pay.percent, "R-COPAY-01"))
if hospital.network_status == "non_network": cands.append((rules.non_network_co_pay_percent, "R-COPAY-02"))
cp = Σ pcts if rules.stack_co_pay else max(pcts, default=0)          # stacking is additive (not multiplicative)
base = after_ded (after_deductible) or eligible (before_deductible)
co_pay_amount = q(base * cp / 100)
```
Age = whole years on `admitted_on` (not today). Flag `CO_PAY_SELECTED` records which candidate won. Stacked co-pay is capped at 100%. In `before_deductible` order, S7 then operates on `base − co_pay_amount`.

**S10 Sum insured.** `remaining = sum_insured + bonus_sum − utilised_this_year` (floored at 0); `payable = min(payable_pre, remaining)`. If `remaining == 0` ⇒ `blocked = SUM_INSURED_EXHAUSTED` (R-SI-02), payable 0 — a normal outcome, not an error. If reduced ⇒ flag `SUM_INSURED_CAPPED` (R-SI-01) and the cut is allocated over lines proportionally to their post-co-pay amounts.

**S11 Allocation.** Each money movement (S7, S8, S10) is allocated to lines with `allocate_cents`; `payable_i = allowed_i − ded_i − copay_i − sicut_i`. Remaining invariants fix per-line identity (§2.3 item 4).

## 7. Config / env vars
`CALC_ENGINE_LOG_LEVEL` (info), `CALC_ENGINE_MAX_LINES=2000`, `CALC_ENGINE_STRICT=true` (invariant failure ⇒ 500 + trace instead of a value), `CALC_ENGINE_BATCH_MAX=500`, `CALC_ENGINE_JWT_ISSUER`, `CALC_ENGINE_JWT_AUDIENCE`. Rule schema versions pinned in code. No secrets, no provider keys (CI grep).

## 8. Error handling and edge cases
| Case | Behaviour |
|---|---|
| Float or >2dp amount, negative amount | 422 `validation_error` (never rounded silently) |
| `qty = 0` or `claimed = 0` | line carried at 0, flag, no division |
| Σ line claimed ≠ submitted `claimed_total` | engine recomputes from lines, flag `TOTAL_MISMATCH` (API should have rejected earlier) |
| Missing `days` on room line | `max(1, discharged − admitted)`; same-day = 1; flag `ROOM_DAYS_ASSUMED` |
| Mid-stay room change (ward → single → ICU) | each room/ICU line evaluated with its own rate/days; ICU days only from lines with `room_type=icu` |
| Sub-limit group with `sum_g = 0` | skipped (guard) |
| Unmapped line (`mapped_group=other`, `mapping_source` missing) | flag `UNMAPPED_LINE`, `needs_review` in trace; not excluded silently |
| `mapping_source="agent"` for > 30% of lines | flag `DEGRADED_MAPPING` (reviewer attention; no number change) |
| Pre/post window line with no `service_date` | allowed + `NO_DATE_ON_LINE` |
| Co-pay candidates both present | non-stacking default: higher wins (Ex-5); stacking additive, capped 100% |
| SI exhausted | `blocked=SUM_INSURED_EXHAUSTED`, payable 0 |
| Floater vs individual | API supplies `utilised_this_year` appropriate to basis; engine only reads it |
| Unknown rules keys | rejected at `PolicyRules` (`extra="forbid"`) |
| Maternity/bariatric/newborn | data (`sub_limits`, `exclusions`), never code branches |
| Duplicate `line_ref` | 422 |
| > MAX_LINES | 413 |
| Rounding drift detected | `EngineInvariantError("allocation_drift")` |
| Same input twice | identical bytes (property test) |
| Discharge before admission | 422 |

## 9. Tests

### 9.1 Worked numeric examples (each is a golden JSON + a parametrised test; values are exact)
**Common setup** unless stated: SI ₹5,00,000; bonus 0; utilised 0; room eligible rate = 1% of SI = ₹5,000/day; ICU = 2% = ₹10,000/day; co-pay 10% only when age ≥ 61; non-network co-pay 20%; deductible 0; member age 45; network hospital; proportionate deduction on (groups: doctor_fees, surgeon_fees, anaesthesia, ot_charges, nursing, investigation, procedure_package; exempt: medicine, implant, consumable); order `standard`; no waiting-period hits.

**Ex-1 Clean claim within caps.** Lines: room 3 d × 4,500 = 13,500; surgeon 40,000; OT 20,000; medicines 18,000; investigations 6,500. Claimed 98,000.
- S4: 4,500 ≤ 5,000 → no cut. S6 eligible 98,000. S8 cp 0. S10 remaining 500,000 → no cap.
- **Payable 98,000.00; patient pays 0.00; deductions none.** remaining SI after = 402,000.

**Ex-2 Room rent excess → proportionate deduction.** Room 4 d × 7,500 = 30,000; surgeon 50,000; anaesthesia 10,000; OT 25,000; medicines 30,000; implant 40,000. Claimed 185,000.
- S4: rate 7,500 > cap 5,000 → room allowed 5,000 × 4 = 20,000 (cut 10,000, R-ROOM-01). ratio = 5,000/7,500 = 2/3 (full precision).
- Proportionate on surgeon/anaesthesia/OT (medicine, implant exempt): exact 33,333.333…, 6,666.666…, 16,666.666… ; target = q(56,666.666…) = 56,666.67. Floors 33,333.33 + 6,666.66 + 16,666.66 = 56,666.65 → 2 cents short → remainders .333…/.666…/.666…(×0.01) → to anaesthesia and OT (largest; tie broken by line_ref). Result: surgeon 33,333.33; anaesthesia 6,666.67; OT 16,666.67.
- Eligible = 20,000 + 33,333.33 + 6,666.67 + 16,666.67 + 30,000 + 40,000 = **146,666.67**. cp 0.
- Deductions: room 10,000.00; surgeon 16,666.67; anaesthesia 3,333.33; OT 8,333.33 = 38,333.33. Check 185,000.00 − 38,333.33 = 146,666.67 ✔. **Payable 146,666.67; patient pays 38,333.33.**

**Ex-3 Sub-limit then senior co-pay.** Member age 66. Knee-replacement group (limit 150,000): implant 120,000 + surgeon 60,000 + OT 25,000 = 205,000. Room 5 d × 4,000 = 20,000; medicines 15,000. Claimed 240,000.
- S3: sum_g 205,000 > 150,000. Exact: implant 87,804.878…, surgeon 43,902.439…, OT 18,292.682… ; floors 87,804.87 + 43,902.43 + 18,292.68 = 149,999.98 → 2 cents short.
  Fractional-cent remainders (exact amount in cents minus floor): implant 8,780,487.8 → .8; surgeon 4,390,243.9 → .9; OT 1,829,268.2 → .2. The two largest (surgeon .9, implant .8) get the extra cent → implant 87,804.88, surgeon 43,902.44, OT 18,292.68. Sum 150,000.00 ✔. (R-SUB-01 deduction total 55,000.)
- S4 no cut (4,000 ≤ 5,000). Eligible = 150,000 + 20,000 + 15,000 = 185,000.
- S8: age 66 ≥ 61 → 10% of 185,000 = 18,500. **Payable 166,500.00; patient pays 73,500.00** (55,000 + 18,500).

**Ex-4 Deductible vs co-pay ordering.** Eligible after caps 100,000 (single line); deductible 10,000; co-pay 10% (age 62).
- `after_deductible` (default): 100,000 − 10,000 = 90,000; co-pay 9,000; **payable 81,000.00**.
- `before_deductible`: co-pay 10,000 on 100,000 → 90,000; deductible 10,000 → **payable 80,000.00**.
Both asserted by two goldens (`ex04a`, `ex04b`).

**Ex-5 Non-network and senior co-pay: stacking toggle.** Age 66 and non-network; eligible 100,000.
- Non-stacking: max(10, 20) = 20% → **payable 80,000.00**, flag `CO_PAY_SELECTED(R-COPAY-02)`.
- `stack_co_pay=true`: 10 + 20 = 30% → **payable 70,000.00**, rule R-COPAY-03.

**Ex-6 Sum-insured cap.** SI 200,000; utilised 150,000 → remaining 50,000. Eligible 80,000 (one medicine line), no co-pay.
- **Payable 50,000.00**, patient pays 30,000.00, flag `SUM_INSURED_CAPPED`, rule R-SI-01, remaining after = 0.

**Ex-7 Pre-existing waiting period (group block, rest paid).** Member declared E11 (diabetes) pre-existing; cover_start = admitted_on − 400 days; waiting 730. Lines: appendectomy group (`procedure_group="appendectomy"`) surgeon 50,000 + OT 20,000 + medicines 10,000 = 80,000; diabetic management group (`diabetes_management`): insulin 8,000 + endocrine consult 4,000 = 12,000. Claimed 92,000. Diagnoses K35.8 and E11.9.
- S1: E11 matches, 400 < 730 → block group `diabetes_management` (R-WAIT-02, flag `WAITING_PERIOD_HIT`). Group lines disallowed 12,000.
- Eligible 80,000 → **payable 80,000.00; patient pays 12,000.00**. If `pre_existing_group_map` had no entry for E11 the whole claim blocks: payable 0, `blocked=PRE_EXISTING_WAITING` (golden `ex07b`).

**Ex-8 Exclusions before sub-limits.** Knee group (limit 150,000): surgeon 90,000 and a cosmetic-tagged line 80,000 (`exclusion_tags=["cosmetic"]`); medicines 5,000. Claimed 175,000.
- S2: cosmetic line excluded (R-EXCL-02), 80,000 disallowed. S3: group members after exclusion = surgeon 90,000 ≤ 150,000 → **no scaling** (if the order were wrong, 170,000 > 150,000 would wrongly scale surgeon down).
- Eligible 95,000 → **payable 95,000.00; patient pays 80,000.00**.

**Ex-9 ICU and ward: single applied ratio.** Ward 3 d × 6,000 = 18,000 (cap 5,000, ratio 5/6); ICU 2 d × 15,000 = 30,000 (cap 10,000, ratio 2/3); surgeon 60,000; medicines 25,000. Claimed 133,000.
- Room allowed 5,000 × 3 = 15,000 (cut 3,000, R-ROOM-01); ICU allowed 10,000 × 2 = 20,000 (cut 10,000, R-ROOM-02). Applied ratio = min(5/6, 2/3) = 2/3 (D-4). Surgeon 60,000 × 2/3 = 40,000 (cut 20,000, R-PROP-01). Medicines exempt.
- Eligible = 15,000 + 20,000 + 40,000 + 25,000 = 100,000 → **payable 100,000.00; patient pays 33,000.00** (3,000 + 10,000 + 20,000).

**Ex-10 Ambulance cap and pre-hospitalisation window.** Admitted 2026-09-10. Lines: surgeon 30,000; medicines 10,000; ambulance 5,000 (cap 3,000); pre-hospitalisation investigation dated 2026-07-31 (41 days before; window 30) 4,000; pre-hospitalisation investigation dated 2026-09-01 (9 days) 3,000. Claimed 52,000.
- R-CAP-01 cut 2,000; R-CAP-02 cut 4,000. Eligible = 30,000 + 10,000 + 3,000 + 3,000 = 46,000. **Payable 46,000.00; patient pays 6,000.00.**

**Ex-11 Initial waiting period vs accident.** Cover started 20 days before admission (initial = 30).
- Planned admission, dx K80.2: `blocked=INITIAL_WAITING` (R-WAIT-01), payable 0.
- Emergency admission, dx S72.0 (fracture; prefix `S` in `accident_icd_prefixes`): block does not apply; normal calculation continues.

**Ex-12 Rounding stress: deductible allocation.** Three lines of eligible 33,333.34 / 33,333.33 / 33,333.33 (Σ 100,000.00); deductible 10,000; no co-pay.
- Exact shares 3,333.334 / 3,333.333 / 3,333.333 → floors 3,333.33 each (9,999.99) → 1 cent to the largest remainder (line 1). Result 3,333.34 / 3,333.33 / 3,333.33 = 10,000.00.
- Line payables: 30,000.00 each. **Payable 90,000.00.** Asserts Σ lines = total exactly and tie-break by `line_ref`.

**Ex-13 Exhausted sum insured.** SI 100,000, utilised 100,000, any lines → **payable 0.00, `blocked=SUM_INSURED_EXHAUSTED`** (R-SI-02), patient pays = claimed; HTTP 200 (a result, not an error).

**Ex-14 Full stack.** SI 300,000, bonus 30,000, utilised 20,000 → remaining 310,000; room cap 1% = 3,000/day; non-network; member age 70; deductible 5,000. Lines: room 5 d × 4,500 = 22,500; surgeon 45,000; anaesthesia 9,000; OT 18,000; medicines 40,000; implant 60,000; cosmetic-tagged 8,000; ambulance 4,000 (cap 3,000). Claimed 206,500.
- S2: cosmetic 8,000 excluded. S4: ratio 3,000/4,500 = 2/3 → room 15,000; surgeon 30,000; anaesthesia 6,000; OT 12,000 (medicine, implant exempt). S5: ambulance 3,000.
- S6 eligible = 15,000 + 30,000 + 6,000 + 12,000 + 40,000 + 60,000 + 3,000 = 166,000. S7: 161,000. S8: candidates age 10% / non-network 20% → 20% → 32,200 → 128,800. S10 cap 310,000 → none.
- **Payable 128,800.00; patient pays 77,700.00** = room 7,500 + surgeon 15,000 + anaesthesia 3,000 + OT 6,000 + exclusion 8,000 + ambulance 1,000 + deductible 5,000 + co-pay 32,200 ✔.

**Ex-15 Order-profile divergence (D-3).** Knee group limit 150,000: surgeon 100,000, OT 60,000, implant 40,000 (Σ 200,000). Room actual 6,250/day vs cap 5,000 → ratio 0.8 (applies to surgeon and OT).
- `standard` (S3 then S4): scale 0.75 → surgeon 75,000, OT 45,000, implant 30,000; then ×0.8 on surgeon/OT → 60,000 and 36,000. Group = 60,000 + 36,000 + 30,000 = **126,000.00**.
- `room_first` (S4 then S3): surgeon 80,000, OT 48,000, implant 40,000 = 168,000 > 150,000 → scale 150/168: 71,428.57 / 42,857.14 / 35,714.29 (cent-remainder to implant) = **150,000.00**.
Same input, different profile → different payable; both asserted; flag `ORDER_PROFILE_NON_DEFAULT` present for `room_first`. Reviewers/policy owners must pick the profile matching the real policy wording.

### 9.2 Golden test table (`tests/golden/`)
| File | Scenario | Expected payable | Expected flags / blocked |
|---|---|---|---|
| ex01 | clean | 98,000.00 | none |
| ex02 | room excess + proportionate | 146,666.67 | none |
| ex03 | sub-limit + senior co-pay | 166,500.00 | none |
| ex04a / ex04b | deductible/co-pay order | 81,000.00 / 80,000.00 | `ORDER_PROFILE_NON_DEFAULT` on b |
| ex05a / ex05b | co-pay stacking off/on | 80,000.00 / 70,000.00 | `CO_PAY_SELECTED` |
| ex06 | SI cap | 50,000.00 | `SUM_INSURED_CAPPED` |
| ex07a / ex07b | pre-existing group / whole-claim | 80,000.00 / 0.00 | `WAITING_PERIOD_HIT`; b: `PRE_EXISTING_WAITING` |
| ex08 | exclusion before sub-limit | 95,000.00 | none |
| ex09 | ICU+ward ratio | 100,000.00 | none |
| ex10 | ambulance + window | 46,000.00 | none |
| ex11a / ex11b | initial waiting / accident exception | 0.00 / calculated | `INITIAL_WAITING` on a |
| ex12 | rounding stress | 90,000.00 | none |
| ex13 | SI exhausted | 0.00 | `SUM_INSURED_EXHAUSTED` |
| ex14 | full stack | 128,800.00 | `CO_PAY_SELECTED` |
| ex15a / ex15b | order profiles | group 126,000.00 / 150,000.00 | `ORDER_PROFILE_NON_DEFAULT` on b |
| edge01 | premium in grace | calculated | `PREMIUM_IN_GRACE` |
| edge02 | premium lapsed beyond grace | 0.00 | `PREMIUM_LAPSED` |
| edge03 | admission before cover_start | 0.00 | `OUTSIDE_COVER` |
| edge04 | all lines excluded | 0.00 | `ALL_LINES_EXCLUDED` |
| edge05 | no date on pre-hosp line | allowed | `NO_DATE_ON_LINE` |
| edge06 | room days missing | days assumed | `ROOM_DAYS_ASSUMED` |
| edge07 | Σ lines ≠ claimed_total | recomputed | `TOTAL_MISMATCH` |
| edge08 | unmapped line | allowed | `UNMAPPED_LINE` |
| edge09 | day-care cataract package | package − sub-limit 40,000 | none |
| edge10 | ≥ 30% agent-mapped | unchanged | `DEGRADED_MAPPING` |

### 9.3 Property-based tests (hypothesis; ≥ 500 examples each in CI, 10,000 nightly)
Strategy: random but valid `CalcInput` (1-40 lines, amounts in whole paise ≤ ₹10,00,000, random rules within schema bounds).
1. `0 ≤ payable ≤ claimed` and `payable ≤ remaining SI`.
2. `Σ line.payable == payable_total`; per-line identity (§2.3 item 4).
3. Idempotence: same input → equal output (and equal JSON bytes).
4. Permutation invariance: shuffled `lines` → same totals; per-line results equal by `line_ref`.
5. Monotonicity: increasing one line's `claimed_amount` never *decreases* `patient_pays_total` and never *increases* `payable_total` beyond the increase.
6. Scale: with all caps/limits/deductible multiplied by k and amounts multiplied by k (integer), payable multiplies by k (±1 cent per line).
7. Zero-deductible/zero-co-pay/no-cap policy ⇒ payable = Σ non-excluded claimed.
8. Raising SI never lowers payable; raising co-pay % never raises payable; raising deductible never raises payable.
9. Excluding more lines never raises payable.
10. `allocate_cents`: Σ output == q(total); each output within 1 cent of exact; deterministic tie-break.
11. No negative or > claimed amount per line.
12. Mid-claim blocked results always have payable 0 and every line `disallowed == claimed`.

### 9.4 Differential test
`tests/reference_calc.py`: independent, naive implementation (loops, `fractions.Fraction` instead of `Decimal`, rounding only at the very end per line, step order from this doc), written without importing engine code. 1,000 random inputs: payable equal within ≤ 1 cent × number of lines **and** identical blocked/flags; any mismatch beyond tolerance fails CI and is triaged as either an engine bug, a reference bug, or a spec gap (update §6).

### 9.5 `explain` CLI output format
```
ENGINE 1.0.0+3fa9c1e  RULES v12  case 0191f0a2
S2  exclusions        206,500.00 → 198,500.00  (-8,000.00)  R-EXCL-02 L0007 cosmetic
S4  room rent         198,500.00 → 191,000.00  (-7,500.00)  R-ROOM-01 L0001 rate 4,500 > cap 3,000 × 5 d
S4  proportionate     191,000.00 → 176,000.00  (-15,000.00 -3,000.00 -6,000.00) R-PROP-01 ratio 0.666667
S5  caps              176,000.00 → 175,000.00  (-1,000.00)  R-CAP-01 ambulance
...
PAYABLE 128,800.00   PATIENT PAYS 77,700.00   FLAGS: CO_PAY_SELECTED(R-COPAY-02)
```
Test: snapshot test of `explain` for ex02 and ex14.

### 9.6 Other tests
- Per-step edge tables (≥ 8 cases each) for S0-S10; rounding tables (0.005 boundaries, tie-breaks, 1-cent drift).
- Rules schema: unknown key rejected, missing required key rejected, bad percent rejected.
- API: float rejected, > MAX_LINES 413, batch with one invalid item returns a ProblemDetail in position, auth required, 500 on forced invariant failure returns trace.
- Mutation testing: `mutmut` ≥ 90% killed on `steps/` and `money.py`.
- Performance: `/v1/calculate` p95 < 50 ms at 200 lines (pytest-benchmark), batch of 500 < 10 s.
- Replay: eval harness (05-integration 02) re-runs all stored `CalcInput`s of a case set against a new engine version and diffs payable; any diff requires the version bump rule (§5 task 13).

## 10. Acceptance criteria
- [ ] Ex-1…Ex-15 and edge01-edge10 pass with exact values to the paisa.
- [ ] 100% branch coverage; mutation score ≥ 90% on `steps/` and `money.py`.
- [ ] All twelve property tests pass at 10,000 examples; differential test zero out-of-tolerance mismatches over 1,000 cases.
- [ ] `explain` prints the §9.5 format for each example.
- [ ] `/v1/calculate` p95 < 50 ms for 200 lines.
- [ ] No float in package (CI grep) and no network/DB/clock imports in `steps/` (import-linter contract).
- [ ] §6.3 order and decisions D-1…D-4 reviewed and ticked by both developers in the PR.
- [ ] `engine_version` and `rules_version` appear in every result and are stored by the API with the calculation row.

## 11. Dependencies
Policy rules schema and resolution (01-03); `Deduction`, `ClaimType`, `AdmissionType` (01-02); insurer-api builds `CalcInput` and persists results (03 §calculation step); crew calc-mapper output (08) feeds `mapped_group`, `is_non_medical`, `is_implant`; decision gate (04) consumes `payable_total`, `flags`, `blocked`. Eval harness (05-integration 02) uses `/v1/calculate/batch`.

## 12. Claude Code kickoff prompt
> Read docs/implementation/03-dev-B-insurer/07-calc-engine.md and docs/implementation/01-shared-contract/03-config-versioning.md only. Enter plan mode first. Then implement tasks 1-14 in insurer/calc_engine/ in this order: money.py (+ tests), models/rules_schema, golden tests from §9.1 (written BEFORE the steps), steps S0→S12, orchestrator, invariants, API wrapper, explain CLI, property tests, reference implementation. No LLM, no I/O, no float inside the engine. Use exact Decimal values from §9.1. Stop and ask if a worked example contradicts §6. Report progress against §10.
