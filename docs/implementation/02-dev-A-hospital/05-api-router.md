# 02-05 — hospital-api: Router (claim type, admission type, flags, pipeline path)

Status: PROPOSED. Owner: Dev A. Code: `hospital/api/app/router_engine/`.

## 1. Goal
The architecture says one configurable pipeline is driven by a single claim-type flag. The router is the deterministic component that, at case creation and after each material change, decides: pipeline path (cashless vs reimbursement), admission flavour (planned vs emergency), derived flags (medico-legal, implant, day-care, maternity, high-value), which config versions apply, and which deadlines/SLAs start.

Design stance:
- **One pipeline, many parameters.** There is no `if cashless:` branching scattered through the codebase. Everything downstream (completeness rules, n8n branches, UI step bar) reads the `RouteDecision` — the claim type only selects a `step_template` and rule applicability.
- **Rules are data.** Router rules live in the `router_rules` config domain (01-03), written in a small closed predicate language; no `eval`.
- **Proposals, not dictates.** The desk user's selection at case creation is a proposal. The router can disagree; disagreements become warnings requiring acknowledgement, never silent changes.
- **Stable and idempotent.** Re-running with the same facts yields the identical decision and writes nothing.

### 1.1 What the router decides, at a glance
| Output | Possible values | Used by |
|---|---|---|
| `pipeline` | `cashless`, `reimbursement` | step template, completeness applicability, submission payload `claim_type` |
| `admission_type` | `planned`, `emergency` | intimation deadline, `R-EMG-01` rules |
| `flags` | `implant`, `high_value`, `medico_legal`, `day_care`, `maternity`, `preauth_issue`, `late_filing`, `provisional` | doc requirements, UI badges, signoff strictness |
| `procedure_group` | e.g. `ortho_implant`, `cardiac_stent`, `null` | doc requirements |
| `required_steps` | ordered list from `step_templates` | n8n flow branching, UI progress bar |
| `filing_deadline` / `intimation_deadline` | UTC timestamps or null | reminders (doc 04 §5.11, doc 08) |
| `config_versions` | the four hospital config versions pinned to the case | every downstream decision stamp |

## 2. Inputs / Outputs
- In: case form data, documents typed so far, parse facts (e.g. "FIR present"), `router_rules` config.
- Out: `RouteDecision` stored on the case (`claim_case.route jsonb` — add in migration 0012) and returned in case detail; audit `router.decided`; drives completeness rule applicability and n8n branch.

```json
{"pipeline":"cashless","admission_type":"emergency","flags":["implant","high_value"],
 "procedure_group":"cardiac_stent","filing_deadline":null,"intimation_deadline":"2026-09-29T10:00:00Z",
 "required_steps":["preauth_check","completeness","claim_build","officer_signoff","submit"],
 "config_versions":{"router_rules":2,"doc_requirements":3,"deadlines":1,"confidence_gates":1}}
```

### 2.1 Fact sources (the `facts` dict)
| Fact key | Type | Source | Available when |
|---|---|---|---|
| `claim_type_proposed` | str | case create form | at create |
| `preauth_ref_present` | bool | form `preauth_ref` non-empty | at create |
| `preauth_valid` | bool/None | `simulated_preauth` lookup | after lookup |
| `hospital_network` | bool | `network_insurer.cashless_supported` joined on `policy_ref.insurer_name` | at create |
| `admission_source` | str | form (`ER`, `OPD`, `referral`) | at create |
| `admission_note_text_flags` | set[str] | parse facts, e.g. `contains_emergency` | after parse |
| `admitted_at` / `discharged_at` | timestamptz | form | when entered |
| `stay_days` | int | `discharged_on - admitted_on` | when both dates exist |
| `icd10_codes`, `icd10_prefixes` | list/set | diagnosis form + discharge summary parse | when entered |
| `procedure_codes` | list | form + parse | when entered |
| `procedure_group` | str/None | `derive_procedure_group()` | when procedure codes exist |
| `claimed_amount` | Decimal/None | latest `claim_draft.totals.claimed` or bill parse | after bill parsed |
| `doc_types_present` | set[str] | documents table | continuous |

## 3. Data model
Add column `claim_case.route jsonb`, `claim_case.flags text[]`. Config `router_rules` payload (PROPOSED):
```json
{"claim_type_rules":[
  {"if":{"preauth_ref_present":true,"hospital_network":true},"then":"cashless"},
  {"if":{"default":true},"then":"reimbursement"}],
 "emergency_rules":{"intimation_hours":24,"signals":["admission_note.contains_emergency","admission_source=ER"]},
 "flag_rules":[
  {"flag":"implant","if":{"procedure_group":["ortho_implant","cardiac_stent"]}},
  {"flag":"high_value","if":{"claimed_amount_gte":500000}},
  {"flag":"medico_legal","if":{"icd10_prefix":["S","T","X","Y"],"or_doc_type_present":["fir_mlc"]}},
  {"flag":"day_care","if":{"stay_days_lte":0}},
  {"flag":"maternity","if":{"icd10_prefix":["O"]}}],
 "step_templates":{"cashless":["preauth_check","completeness","claim_build","officer_signoff","submit"],
                   "reimbursement":["completeness","claim_build","officer_signoff","submit","filing_window_check"]}}
```

### 3.1 DDL (migration `0012_router`)
```sql
ALTER TABLE claim_case
  ADD COLUMN route        jsonb,
  ADD COLUMN flags        text[] NOT NULL DEFAULT '{}',
  ADD COLUMN admitted_at  timestamptz,
  ADD COLUMN discharged_at timestamptz,
  ADD COLUMN converted_from uuid REFERENCES claim_case(id),
  ADD COLUMN converted_to   uuid REFERENCES claim_case(id);

CREATE TABLE network_insurer (
  insurer_name       text PRIMARY KEY,
  cashless_supported boolean NOT NULL DEFAULT false,
  notes              text,
  updated_by         uuid REFERENCES app_user(id),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE simulated_preauth (
  ref              text PRIMARY KEY,
  member_id        text NOT NULL,
  insurer_name     text NOT NULL,
  approved_amount  numeric(14,2) NOT NULL,
  valid_from       date NOT NULL,
  valid_to         date NOT NULL,
  status           text NOT NULL CHECK (status IN ('approved','expired','revoked','pending'))
);

CREATE TABLE route_history (
  id          uuid PRIMARY KEY DEFAULT gen_uuid_v7(),
  case_id     uuid NOT NULL REFERENCES claim_case(id),
  seq         int  NOT NULL,
  decision    jsonb NOT NULL,
  decision_hash char(64) NOT NULL,
  trigger     text NOT NULL,        -- create|patch|doc_classified|draft_totals|config_republish|manual|override
  actor       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE(case_id, seq)
);
```

### 3.2 Predicate language (closed set)
Predicates appear only inside `"if"` objects. An `if` object is an implicit **AND** of its keys.

| Key | Meaning | Operand | Example |
|---|---|---|---|
| `default` | always true | `true` | `{"default":true}` |
| `<fact>` | equality or membership | scalar or list | `{"admission_source":"ER"}` |
| `<fact>_gte` / `_lte` / `_gt` / `_lt` | numeric compare (Decimal) | number | `{"claimed_amount_gte":500000}` |
| `icd10_prefix` | any code starts with any prefix | list[str] | `{"icd10_prefix":["O"]}` |
| `procedure_group` | membership | list[str] | |
| `contains` | any of values in a set fact | list | `{"admission_note_text_flags":{"contains":["contains_emergency"]}}` |
| `or_doc_type_present` | OR-combined with sibling keys | list[DocType] | see `medico_legal` rule |
| `any` / `all` / `not` | nesting | list/dict | `{"any":[{...},{...}]}` |

Evaluation semantics: unknown fact → predicate evaluates to `UNKNOWN`; `UNKNOWN` is treated as false in `claim_type_rules` (falls through to next rule) and false in `flag_rules`, but the decision is marked `provisional=true` when any predicate result in a rule that would have changed the outcome was `UNKNOWN`. Validation (at publish) rejects unknown predicate keys, non-list operands where lists are required, and `default` anywhere but the last claim-type rule.

## 4. API / endpoints
```
GET  /v1/cases/{id}/route                 current decision
POST /v1/cases/{id}/route/recompute       officer; returns diff vs previous
POST /v1/cases/{id}/route/override        officer {claim_type?, admission_type?, flags_add?, flags_remove?, reason}
POST /v1/cases/{id}/convert               officer {to:"reimbursement", reason}   (see task 10)
Admin: /v1/admin/config/router_rules/* (same lifecycle as doc 04 §4)
Internal: POST /v1/internal/cases/{id}/route/recompute (svc-n8n, svc-crew)
```
Override changes are always audited with reason and cannot remove `medico_legal` if an FIR doc is present (409 `override_conflicts_evidence`).

### 4.1 Example: `GET /v1/cases/{id}/route`
```json
{
  "case_id": "6f1c0d2e-3b7a-7d11-a8c4-5a2b9c3d1e90",
  "seq": 3,
  "provisional": false,
  "decision": {
    "pipeline": "cashless",
    "admission_type": "emergency",
    "flags": ["high_value", "implant"],
    "procedure_group": "cardiac_stent",
    "intimation_deadline": "2026-09-29T10:00:00Z",
    "filing_deadline": null,
    "required_steps": ["preauth_check","completeness","claim_build","officer_signoff","submit","intimation_check"],
    "config_versions": {"router_rules":2,"doc_requirements":3,"deadlines":1,"confidence_gates":1}
  },
  "warnings": [
    {"code":"claim_type_disagrees_with_selection","message":"Desk selected reimbursement; rules say cashless (insurer in network, valid pre-auth).","needs_ack":true}
  ],
  "proposal": {"claim_type":"reimbursement","by":"u-desk-04"},
  "ack": null
}
```

### 4.2 Example: `POST /v1/cases/{id}/route/recompute`
Response `200` shows the diff:
```json
{"changed": true, "from_seq": 2, "to_seq": 3,
 "diff": {"flags": {"added": ["implant"], "removed": []},
          "required_steps": {"added": ["intimation_check"], "removed": []},
          "pipeline": null},
 "completeness_run_scheduled": true}
```
Unchanged → `{"changed": false, "seq": 3}` with no audit and no history row.

### 4.3 Example: `POST /v1/cases/{id}/route/override`
Request:
```json
{"claim_type":"reimbursement","flags_remove":["high_value"],"reason":"Patient opted out of cashless after TPA call on 05-Oct; amount below threshold per final bill"}
```
Rules enforced (in order): role Officer; case status ∈ {`draft`,`docs_pending`,`docs_complete`,`building_claim`,`ready_for_review`} (409 `case_locked` once `submitted`); `reason` ≥ 15 chars; evidence conflicts (`override_conflicts_evidence`); resulting step list must exist in `step_templates`. Response includes the new decision and `override: true` flag; recompute afterwards keeps overrides unless `?clear_overrides=true`.

Override storage: `route.overrides = {claim_type?, admission_type?, flags_add[], flags_remove[], by, reason, at}`; `decide()` applies overrides after rule evaluation so the rule output remains visible in `route.rules_output` for transparency.

### 4.4 Example: convert (cashless denied → reimbursement)
```http
POST /v1/cases/{id}/convert
{"to":"reimbursement","reason":"Insurer declined cashless at final stage; patient will claim post-discharge"}
```
```json
{"new_case_id":"7a22…","new_claim_ref":"HC-2026-000531","linked_from":"HC-2026-000498",
 "documents_copied":14,"original_status":"closed","original_close_reason":"converted"}
```

## 5. Build tasks
1. Schema + JSON Schema for `router_rules`; seed v1. Files: `router_engine/schema.py`, `seeds/router_rules_v1.json`.
2. `router_engine/facts.py`: gather facts dict (claim form values, derived stay days, claimed amount if draft exists, ICD prefixes, doc types present, `hospital_network` from `hospital` table flag `network_with_insurer` — add via policy_ref lookup of insurer name against `network_insurer` seed table `network_insurer(insurer_name, cashless_supported bool)`).
3. `router_engine/evaluate.py`: pure `decide(facts, rules) -> RouteDecision`; condition mini-language implemented as small typed predicates (no `eval`): `eq`, `in`, `gte`, `lte`, `prefix`, `contains`, `any`, `all`, `not`.
4. Deadline computation: reimbursement `filing_deadline = discharged_on + deadlines.reimbursement_filing_days` (PROPOSED default 30); emergency cashless `intimation_deadline = admitted_at + 24 h`; produce reminders list for doc 08.
5. Procedure-group derivation shared with doc 04 (single function `derive_procedure_group(codes, groups_map)` in `common/procedures.py`).
6. Persistence service: recompute triggers — case create, case PATCH of relevant fields, document classified (new `fir_mlc`), draft totals change, config republish (only for cases in `draft/docs_pending`).
7. Cashless pre-auth check step (simulated per scope): `preauth_check` looks up `preauth_ref` in `simulated_preauth` table (seeded; fields: ref, member_id, approved_amount, valid_to, status). Mismatch (member, expired, amount lower than expected) → flag `preauth_issue` warning shown to officer; does NOT block.
8. Route diff + audit event; SSE `case.route_changed`.
9. Override endpoint with evidence-conflict rules.
10. Switching claim type mid-case (cashless → reimbursement after insurer denies cashless): `POST /v1/cases/{id}/convert` creates linked successor case copying documents by reference (hash same), keeps history, closes original with reason `converted`. PROPOSED.
11. Expose route in n8n via `GET` so flows branch on `pipeline` and `required_steps`.
12. Unit-test DSL parser/validator; reject unknown predicates at config validation.

### 5.1 Step detail: predicate engine (task 3)
```python
@dataclass(frozen=True)
class Tri:  # three-valued logic: True / False / UNKNOWN
    v: bool | None


TRUE, FALSE, UNKNOWN = Tri(True), Tri(False), Tri(None)


def holds(cond: dict, facts: dict) -> Tri:
    if cond.get("default") is True:
        return TRUE
    results = []
    for key, operand in cond.items():
        if key == "or_doc_type_present":  # handled by the caller as OR with siblings
            continue
        results.append(eval_key(key, operand, facts))
    base = and_(results) if results else TRUE
    if "or_doc_type_present" in cond:
        present = bool(set(cond["or_doc_type_present"]) & facts.get("doc_types_present", set()))
        return or_(base, TRUE if present else FALSE)
    return base


def eval_key(key, operand, facts) -> Tri:
    if key in ("any", "all"):
        subs = [holds(c, facts) for c in operand]
        return or_(*subs) if key == "any" else and_(*subs)
    if key == "not":
        r = holds(operand, facts)
        return UNKNOWN if r.v is None else Tri(not r.v)
    fact, op = split_suffix(key)  # "claimed_amount_gte" -> ("claimed_amount", "gte")
    if fact == "icd10_prefix":  # special: operand list of prefixes
        codes = facts.get("icd10_codes")
        if codes is None:
            return UNKNOWN
        return Tri(any(c.startswith(tuple(operand)) for c in codes))
    if fact not in facts or facts[fact] is None:
        return UNKNOWN
    return COMPARATORS[op](facts[fact], operand)  # eq|in|gte|lte|gt|lt|contains
```
`and_`: any False → False; else any UNKNOWN → UNKNOWN; else True. `or_`: any True → True; else any UNKNOWN → UNKNOWN; else False.

### 5.2 Step detail: `decide()` complete form
```python
def decide(facts, rules, overrides=None) -> RouteDecision:
    provisional = False

    # 1. claim type: first matching rule wins; UNKNOWN falls through but marks provisional
    claim_type = None
    for r in rules.claim_type_rules:
        t = holds(r["if"], facts)
        if t.v is True:
            claim_type = r["then"]
            break
        if t.v is None:
            provisional = True
    # (validation guarantees a trailing {"default":true} rule, so claim_type is never None)

    # 2. admission type: emergency wins on any TRUE signal
    sigs = [signal_holds(s, facts) for s in rules.emergency_rules["signals"]]
    adm = "emergency" if any(s.v is True for s in sigs) else "planned"
    provisional |= adm == "planned" and any(s.v is None for s in sigs)

    # 3. flags (stable order: sorted)
    flags = set()
    for fr in rules.flag_rules:
        t = holds(fr["if"], facts)
        if t.v is True:
            flags.add(fr["flag"])
        elif t.v is None:
            provisional = True
    if facts.get("preauth_issue"):
        flags.add("preauth_issue")
    if provisional:
        flags.add("provisional")

    # 4. overrides applied last, rules output retained for transparency
    rules_out = {"pipeline": claim_type, "admission_type": adm, "flags": sorted(flags)}
    if overrides:
        claim_type = overrides.get("claim_type", claim_type)
        adm = overrides.get("admission_type", adm)
        flags = (flags | set(overrides.get("flags_add", []))) - set(
            overrides.get("flags_remove", [])
        )

    # 5. steps
    steps = list(rules.step_templates[claim_type])
    if adm == "emergency" and claim_type == "cashless":
        steps.append("intimation_check")

    # 6. deadlines
    dl = deadlines(claim_type, adm, facts, rules)  # see 5.3

    return RouteDecision(
        pipeline=claim_type,
        admission_type=adm,
        flags=sorted(flags),
        procedure_group=facts.get("procedure_group"),
        required_steps=steps,
        provisional=provisional,
        rules_output=rules_out,
        **dl,
    )
```

### 5.3 Step detail: deadlines (task 4)
| Case | Formula | Config key | Default |
|---|---|---|---|
| Reimbursement filing | `discharged_at.date() + reimbursement_filing_days` at 23:59:59 Asia/Kolkata → stored UTC | `deadlines.reimbursement_filing_days` | 30 |
| Emergency cashless intimation | `admitted_at + emergency_rules.intimation_hours` | `emergency_rules.intimation_hours` | 24 |
| Planned cashless pre-auth lead | `admitted_at − preauth_lead_hours` (warning only) | `deadlines.planned_preauth_lead_hours` | 48 |
| Query response SLA | from query `due_by` (doc 07) | `query_policy.default_sla_hours` | 72 |

If `discharged_at` is unknown for reimbursement, `filing_deadline=null` and `provisional=true`. Reminder list produced: `[{kind:"filing_deadline", at: deadline-7d}, {at: deadline-2d}, {at: deadline-6h}]`, consumed by the n8n reminder flow (doc 08). Past-due deadlines add flag `late_filing` (reimbursement) which is carried into the submission as a reason requirement (doc 06).

### 5.4 Step detail: pre-auth check (task 7)
```python
def preauth_check(case, sim_rows) -> PreauthResult:
    row = sim_rows.get(case.preauth_ref)
    if not row:
        return PreauthResult("not_found", warn="Pre-auth reference not found")
    issues = []
    if row.member_id != case.patient.member_id:
        issues.append("member_mismatch")
    if row.status != "approved":
        issues.append(f"status_{row.status}")
    if not (row.valid_from <= case.admitted_on <= row.valid_to):
        issues.append("outside_validity")
    if case.expected_amount and case.expected_amount > row.approved_amount * Decimal("1.10"):
        issues.append("expected_exceeds_preauth")
    return PreauthResult("ok" if not issues else "issues", issues=issues)
```
Result adds flag `preauth_issue` and a warning item; it never blocks the pipeline (the real insurer is authoritative; scope excludes pre-auth).

### 5.5 Step detail: recompute triggers (task 6)
| Trigger | Where fired | Debounce | Notes |
|---|---|---|---|
| `create` | `POST /cases` | none | initial decision, uses proposal |
| `patch` | `PATCH /cases/{id}` touching `diagnosis`, `procedure_codes`, `admitted_*`, `discharged_*`, `preauth_ref`, `policy_ref` | 2 s | |
| `doc_classified` | doc 03 classification callback for `fir_mlc`, `admission_note`, `preauth_approval` | 5 s | |
| `draft_totals` | claim draft saved with new `claimed` | none | may add/remove `high_value` |
| `config_republish` | admin publish of `router_rules` | none | only cases in `draft`/`docs_pending`; other cases are untouched |
| `manual` | recompute endpoint | none | |
After `submitted`, recompute still runs (so UI shows current flags) but is recorded as `post_submission` and never mutates `required_steps`.

## 6. Key logic
```python
def decide(facts, rules):
    claim_type = first_match(rules.claim_type_rules, facts).then
    adm = "emergency" if match_any(rules.emergency_rules.signals, facts) else "planned"
    flags = [r.flag for r in rules.flag_rules if holds(r.if_, facts)]
    steps = rules.step_templates[claim_type] + (
        ["intimation_check"] if adm == "emergency" and claim_type == "cashless" else []
    )
    return RouteDecision(claim_type, adm, flags, steps, deadlines(claim_type, adm, facts, rules))
```
User-selected claim type at creation is treated as a *proposal*; router warns if rules disagree (e.g. user chose cashless but insurer not in network) and requires acknowledgement.

### 6.1 Worked examples
**Example A — planned cashless knee replacement**
Facts: `preauth_ref_present=true`, `hospital_network=true`, `admission_source=OPD`, `procedure_codes=[0SRD0J9]` → group `ortho_implant`, `claimed_amount=312000`, `stay_days=4`, ICD `M17.1`.
- claim_type_rules[0] true → `cashless`. Emergency signals: none true → `planned`.
- flags: `implant` ✔ (group), `high_value` ✘ (312,000 < 500,000), `day_care` ✘, others ✘ → `["implant"]`.
- steps: cashless template (no intimation). Deadlines: none (cashless planned; lead-time warning only).

**Example B — emergency cardiac stent, no pre-auth ref**
Facts: `preauth_ref_present=false`, `admission_source=ER`, `procedure_codes=[02703DZ]` → `cardiac_stent`, `claimed_amount=640000`, ICD `I21.0`.
- claim_type_rules: rule 0 false (no ref) → rule 1 default → `reimbursement`; desk proposed `cashless` → warning `claim_type_disagrees_with_selection`, needs acknowledgement (see edge cases for emergency cashless without ref: PROPOSED rule — if emergency and network and `intimation_pending`, route as `cashless` with `intimation_check`; add as an additional first rule in seed v1: `{"if":{"admission_source":"ER","hospital_network":true},"then":"cashless"}`).
- With that seed rule: `cashless`, `emergency`, flags `["high_value","implant"]`, steps `[... , "intimation_check"]`, `intimation_deadline = admitted_at + 24h`.

**Example C — road traffic injury, day-care physio**
Facts: ICD `S72.0` → prefix `S` → `medico_legal`; `stay_days=0` → `day_care`. FIR not yet uploaded: flag set from ICD alone; doc 04 adds rule `R-MLC-01` requiring `fir_mlc`. If later `fir_mlc` classified, flag is retained (ICD still matches) and override-remove is blocked (409).

**Example D — unknown facts**
Create with only name and policy: `icd10_codes=None`, no dates. `claim_type_rules` evaluate (network lookup works) → `reimbursement` or `cashless`; flags rules with ICD predicates → UNKNOWN → decision `provisional=true`, flag `provisional`. UI shows "Route may change as details are entered". Next PATCH recomputes.

## 7. Config / env vars
Everything from DB config. Env: `HOSP_SIM_PREAUTH_ENABLED=true`, `HOSP_ROUTER_DEBOUNCE_S=2`, `HOSP_TZ=Asia/Kolkata` (used only for filing-deadline end-of-day).

## 8. Error handling and edge cases
- Facts missing (no diagnosis codes yet): evaluate with what exists; mark `provisional`.
- Conflicting signals (emergency note but planned selected): emergency wins, warning raised.
- Flags that change after submission: recompute allowed, but only logged (does not alter submitted claim).
- Rule set references unknown flag in doc_requirements: validated across configs during publish dry-run.
- Day-care procedures with same-day admit/discharge: `stay_days=0` valid.
- Network list stale: admin-editable table; unknown insurer defaults to reimbursement with warning.
- Timezones: deadlines computed in UTC from `admitted_at` timestamp (add `admitted_at timestamptz` alongside `admitted_on`).

### 8.1 Edge-case table
| Situation | Behaviour |
|---|---|
| Insurer name typo (`"Star Healt"`) | not in `network_insurer` → reimbursement + warning `insurer_not_in_network_table`; admin can add alias row |
| Override tries to set cashless while `hospital_network=false` | allowed with reason (hospital may have a special arrangement) but warning `override_against_network` is stored and displayed to the submit step |
| Convert after status `submitted` and insurer-side decision `rejected` | allowed; creates successor, original closed `converted`; conversion not allowed from `settled` |
| Convert twice | 409 `already_converted` pointing at successor |
| Router config republished with a removed flag still referenced in `doc_requirements` | publish blocked with `dangling_flag_reference` during cross-config validation |
| `claimed_amount` = exactly threshold (500000) | `gte` → flag set (inclusive) |
| Missing `admitted_at` time component | assume 00:00 local; `intimation_deadline` marked `approximate=true` |
| Two ICD prefixes match two flags | both flags set; flags are sorted lexicographically for stable output |
| Case has `medico_legal` flag and FIR later deleted | flag persists (ICD-derived); completeness shows missing FIR blocker |
| Simulated preauth table empty | `preauth_check` returns `not_found`; warning only |
| Concurrent recompute | row lock on case; second run sees equal hash and returns `changed=false` |

## 9. Tests
Table-driven `decide` tests (≥ 40 combos); DSL fuzz tests; override rules; conversion flow; recompute idempotency (same facts → same decision, no new audit if unchanged); config republish effect on in-flight cases; simulated preauth mismatch cases; integration with doc 04 (flags change applicability).

### 9.1 `decide` test matrix (excerpt)
| # | Proposal | network | preauth ref | source | ICD | proc group | amount | stay | Expected pipeline / adm / flags |
|---|---|---|---|---|---|---|---|---|---|
| R01 | cashless | yes | yes | OPD | M17.1 | ortho_implant | 312000 | 4 | cashless / planned / [implant] |
| R02 | cashless | no | yes | OPD | M17.1 | ortho_implant | 312000 | 4 | reimbursement / planned / [implant] + warn |
| R03 | reimb | yes | no | ER | I21.0 | cardiac_stent | 640000 | 5 | cashless(seed rule) / emergency / [high_value, implant] |
| R04 | cashless | yes | yes | OPD | O80 | — | 80000 | 3 | cashless / planned / [maternity] |
| R05 | any | yes | yes | OPD | H25.9 | — | 45000 | 0 | cashless / planned / [day_care] |
| R06 | any | yes | yes | ER | S72.0 | — | 120000 | 6 | cashless / emergency / [medico_legal] |
| R07 | any | — | — | — | None | None | None | None | per proposal rules / planned / [provisional] |
| R08 | any | yes | yes | OPD | C34.9 | — | 500000 | 7 | high_value inclusive → [high_value] |
| R09 | any | yes | yes | OPD | C34.9 | — | 499999.99 | 7 | [] |
| R10 | any | unknown insurer | yes | OPD | any | any | any | any | reimbursement + warn `insurer_not_in_network_table` |
| R11 | cashless | yes | yes | OPD (note says "emergency admission") | any | | | | emergency wins + warning |
| R12 | any | yes | yes | OPD | any | | | | override flags_remove high_value allowed; medico_legal with FIR present → 409 |
Add 28 more rows covering each predicate operator, `not`, `any`, `all`, UNKNOWN propagation, and step-template selection.

### 9.2 Other test groups
- **DSL fuzz**: Hypothesis generates random condition trees; assert (a) never raises on valid grammar, (b) `holds` obeys three-valued logic laws (De Morgan with UNKNOWN), (c) validator rejects mutated invalid trees.
- **No-eval lint**: `ruff` custom rule / grep test fails build if `eval(`, `exec(`, `compile(` appear under `router_engine/`.
- **Idempotency**: call recompute N times with unchanged facts; assert one `route_history` row and zero extra audit events.
- **Conversion**: documents copied by reference (same `sha256`, new `document` rows pointing at same MinIO object); successor has fresh `claim_ref`; original closed; audit has linked events in both cases.
- **Override evidence**: FIR present + remove `medico_legal` → 409; FIR absent → allowed.
- **Config republish**: in-flight `docs_pending` cases recomputed; `submitted` cases untouched.
- **Preauth**: member mismatch, expired validity, status revoked, amount >110% → `preauth_issue` flag, warnings text, no blocking.
- **Integration with doc 04**: flip `implant` flag via procedure edit → completeness adds `R-IMP-02` item on next run.

## 10. Acceptance criteria
- [ ] Same facts always give identical `RouteDecision`.
- [ ] Changing claim type flag changes the step list and required docs without code changes.
- [ ] No `eval`/`exec` in codebase for rules (lint check).
- [ ] Override and conversion are fully audited.

## 11. Dependencies
Docs 01, 03, 04; config service; seed `network_insurer`, `simulated_preauth`. Consumed by 06, 08, 10.

## 12. Claude Code kickoff prompt
> Implement docs/implementation/02-dev-A-hospital/05-api-router.md. Add the migration for new columns/tables first (extend doc 01 migrations as 0012), then the pure decision engine with tests, then endpoints and triggers.
