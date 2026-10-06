# 05-01 — Synthetic Data Generation and Ground Truth

Status: PROPOSED. Joint ownership. Code lives in `data/synthetic/`; outputs in `data/synthetic/out/` (git-ignored except the 20-case `golden/` set).

## 1. Goal
All members, hospitals, policies, claims and documents are synthetic (architecture §1 assumptions). This doc defines generators that produce (a) realistic-looking PDFs and images for the document pipeline, (b) structured JSON that is the exact ground truth for every field and every expected decision, and (c) controlled defects so that each agent and gate in the pipeline can be measured. Nothing derived from real patient data may enter this repo.

## 2. Inputs / Outputs

### Inputs
- `data/synthetic/seed.yaml`: global seed, counts, defect mix, locale (en-IN).
- Policy catalogue `data/synthetic/catalog/policies.yaml` (products, sub-limits, co-pay, waiting periods, exclusions).
- Procedure catalogue `data/synthetic/catalog/procedures.yaml` (ICD-10 dx, procedure codes, typical LOS, price ranges, required docs, implant flag).
- Hospital catalogue `hospitals.yaml` (name, city, network flag, letterhead style id, stamp style id).
- Templates: Jinja2 HTML → PDF (ReportPaint via WeasyPrint or ReportLab; PROPOSED: ReportLab for deterministic output) in `templates/`.

### Outputs (per case, directory `out/<case_id>/`)
```
case.json                 # ground truth (section 5)
docs/NN_<doc_type>.pdf    # generated documents (clean)
docs/NN_<doc_type>.degraded.{pdf,png}   # after degradation pass, if applicable
docs/NN_<doc_type>.labels.json          # per-document field labels with bbox
expected/hospital.json    # expected completeness / classification / claim-build outputs
expected/insurer.json     # expected identity, authenticity, coverage, calc, decision
expected/queries.json     # scripted query scenario for tpa-sim (if any)
manifest.json             # sha256 of every file, generator version, seed
```
Plus corpus-level `corpus_manifest.json` and `splits.json` (dev / test / golden).

## 3. Data model (generator side)

### 3.1 Entities
- `SyntheticMember`: member_id (`MB` + 9 digits), full_name (Faker en_IN), dob, gender, phone (masked fake `98xxxxxx12` pattern but fully fake), address, id_proof_type (`aadhaar_like`, `pan_like`, `voter_like`) with a **fake** number that passes format but is flagged by prefix `9999` so no real number can collide. PROPOSED: Aadhaar-like numbers start with `9999` plus Verhoeff-valid check digit so validators exercise but are guaranteed non-issued.
- `SyntheticPolicy`: policy_number, product_id, start/end, sum_insured, members[], waiting_period_end per condition, co_pay_pct, room_rent_cap_pct, exclusions[], network_only flag.
- `SyntheticHospital`: hospital_id, name, registration_no (fake), address, network_status, letterhead_style, stamp_style (shape, ink colour, text), doctor roster.
- `SyntheticCase`: ties member, policy, hospital, procedure, admission window, bill, documents, defects, expected outcome.

### 3.2 Bill model
Bill lines follow `BillLine` from `01-shared-contract/02-data-models-and-enums.md`. Generator builds: room (days × tariff by room class), ICU days, surgeon/anaesthesia fees, OT charges, medicines (10-60 lines), consumables, investigations, implants (with sticker docs), consultations. Rounding: Decimal, 2dp, half-up. Itemised total equals final bill total unless the `bill_total_mismatch` defect is applied.

## 4. Generators (build in this order)

### 4.1 `gen_entities.py`
Seeded `random.Random(seed)` and `Faker("en_IN")` seeded with the same value; every function takes an `rng`. Produces members, policies, hospitals from the catalogues. Guarantees uniqueness of member_id and policy_number across the corpus.

### 4.2 `gen_cases.py`
Selects scenario archetype (section 6), picks procedure, computes admission window (LOS ~ procedure LOS ± 1 day Poisson), assembles bill lines, computes expected coverage by calling the **reference calculator** `data/synthetic/reference_calc.py`. IMPORTANT: the reference calculator is an independent re-implementation (not an import of `calc_engine`) so that eval compares two implementations. Differences go to a triage list.

### 4.3 `gen_documents.py`
Document types and fields rendered (all must exist in `DocType`):

| DocType | Key fields rendered | Layout variants |
|---|---|---|
| discharge_summary | patient, dob, IP no, DOA, DOD, dx, procedure, course, medication on discharge, doctor sign | 4 |
| prescription | doctor name/reg no, date, patient, medicines with dose/frequency, doctor stamp or signature | 4 (printed + handwritten variants) |
| procedure_bill | procedure name/code, surgeon, theatre charges, date, hospital stamp | 3 |
| final_bill | header, bill no, patient, dates, grouped totals, net payable, advance, signatory | 4 |
| itemised_bill | tabular lines with codes/qty/rate/amount, page subtotals, grand total | 5 (multi-page 1-6 pages) |
| pharmacy_bill | batch, expiry, MRP, qty | 3 |
| lab_report / radiology_report / investigation_report | test names, values with units/reference ranges, pathologist sign | 3 each |
| admission_note | complaint, vitals, provisional dx | 2 |
| preauth_approval | auth no, approved amount, validity | 2 (simulated, architecture §1 out of scope) |
| claim_form | member info, bank details (fake), declaration, signature | 2 |
| id_proof | card image with photo placeholder (generated avatar), number, name, dob | 3 per type |
| policy_card | policy no, member, validity | 2 |
| cancelled_cheque | bank, IFSC (fake range `FAKE0xxxxxx`), account (fake) | 2 |
| implant_sticker | brand, model, lot, MRP | 3 |
| payment_receipt | amount, mode, date | 2 |
| fir_mlc | only for emergency/accident | 1 |

Each template emits a **label file** with every semantically important field: `{field, value, page, bbox[x0,y0,x1,y1] in PDF points, source_text}`. Labels are produced by the renderer itself (it knows where it drew text), never by OCR.

Stamps and signatures (needed for vision-service): `draw_stamp(style)` renders circular / rectangular / oval stamp with hospital name, registration, date; random rotation ±25°, opacity 0.6-0.95, ink blue/violet/red. Signatures: bezier scribble from seeded random control points. Label `stamp_present`, `stamp_text`, `signature_present` per page with bbox.

### 4.4 `degrade.py` (quality and authenticity defects)
Applied to rasterised pages and re-wrapped to PDF (image-only PDF so there is no text layer, forcing OCR).

| Degradation | Parameters (PROPOSED) | Label written |
|---|---|---|
| gaussian blur | sigma 0.5-3.0 | `blur_sigma` |
| low resolution | 72-150 dpi | `dpi` |
| skew | ±1-8 degrees | `skew_deg` |
| perspective (phone photo) | corner jitter 2-8% | `perspective` |
| shadow / uneven lighting | gradient overlay | `shadow` |
| JPEG artefacts | quality 25-70 | `jpeg_q` |
| partial crop | cut 5-20% off an edge | `cropped_edge`, `lost_fields[]` |
| fold line / stain | overlay | `occlusion_boxes[]` |
| missing stamp | do not draw stamp | `stamp_present=false` |
| faint stamp | opacity <0.25 | `stamp_faint=true` |
| handwriting font | for doctor notes (script font) | `handwritten=true` |
Each degradation level yields an `expected_quality_class` in `{good, acceptable, poor, unreadable}` using a deterministic rule (e.g. blur_sigma>2.2 or dpi<90 → `poor`; cropped losing a required field → `unreadable`).

### 4.5 `tamper.py` (authenticity fraud simulation; insurer Authenticity agent)
| Tamper | Method | Label |
|---|---|---|
| amount edit | overwrite a number region with different font/kerning | `tamper=amount_edit`, bbox |
| date edit | change admission/discharge date | `tamper=date_edit` |
| name swap | replace patient name | `tamper=name_swap` |
| copy-paste stamp | duplicate a stamp from another doc | `tamper=stamp_reuse` |
| template mismatch | letterhead style of hospital B with hospital A name | `tamper=template_mismatch` |
| metadata anomaly | PDF producer/creation date after discharge by >30 days or editor tool name | `tamper=metadata` |
| arithmetic inconsistency | line sum does not match total | `tamper=arith` |
| duplicate invoice | same bill number reused across two claims | `tamper=duplicate_bill_no` |
Ground truth for each tampered case: `expected.authenticity.flag=true`, `reason_codes[]`.

### 4.6 `gen_kb.py`
Generates policy wording PDFs (and markdown) used for RAG ingestion (bucket `kb-sources`): clause-numbered text with sub-limits, exclusions, waiting-period tables, claim procedure, and "query response" templates. Every clause has a stable `clause_id`; coverage ground truth cites clause ids so RAG citation accuracy can be scored.

## 5. Ground truth schema (`case.json`) — PROPOSED

```jsonc
{
  "schema_version": "1.0",
  "case_id": "SYN-000123",
  "seed": 424242,
  "archetype": "clean_cashless_planned",
  "claim_type": "cashless",
  "admission_type": "planned",
  "member": {...}, "policy": {...}, "hospital": {...},
  "admission": {...},                      // Admission model fields
  "bill_lines": [...],                     // BillLine model fields
  "totals": {"gross":"184520.00","discounts":"4520.00","claimed":"180000.00"},
  "documents": [
    {"doc_id":"...", "doc_type":"discharge_summary", "file":"docs/01_discharge_summary.degraded.pdf",
     "pages":2, "quality_class":"acceptable",
     "defects":[{"type":"skew","value":3.2}], "fields":"docs/01_discharge_summary.labels.json"}
  ],
  "defects_applied":["missing_doc:implant_sticker"],
  "expected": {
    "hospital": {
      "classification": [{"doc_id":"...","doc_type":"..."}],
      "completeness": {"complete":false,"missing":["implant_sticker"],"blockers":[], "warnings":[]},
      "router": {"claim_type":"cashless","admission_type":"planned","procedure_group":"ortho_implant"},
      "claim_build_fields": {"patient.full_name":"...","admission.diagnosis_codes":["M17.1"], "...": "..."}
    },
    "insurer": {
      "identity": {"match":true,"score_min":0.9,"mismatches":[]},
      "authenticity": {"flag":false,"reason_codes":[]},
      "coverage": {"in_force":true,"waiting_period_ok":true,"exclusions_hit":[],"clauses":["C-4.2","C-7.1"]},
      "calculation": {"payable":"142000.00","deductions":[{"line_ref":3,"rule_id":"room_rent_cap","amount":"6000.00"}]},
      "decision": {"outcome":"partial","route":"human_reviewer","needs_two_approvers":false},
      "queries": []
    }
  }
}
```
Money always strings. Every `expected` value is derived programmatically, never hand-typed (except golden set adjudications, see 5.1).

### 5.1 Golden set (20 hand-reviewed cases)
Pick one case per archetype from section 6 (plus extras for hard ones). A human (Dev A or B) reviews the generated PDFs and `case.json`, signs `golden/<case>/REVIEWED` with date and initials. Golden cases are frozen: changing generator code must not change their files (checked by sha256 in `golden/manifest.json`).

## 6. Scenario archetypes (each with a defect recipe and expected outcome)

| ID | Archetype | Defects | Expected hospital | Expected insurer |
|---|---|---|---|---|
| S01 | clean_cashless_planned | none | complete → submit | verify OK, payable calculated, reviewer approve |
| S02 | clean_cashless_emergency | none, FIR absent but not required | complete | approve |
| S03 | clean_reimbursement_planned | none | complete incl. cheque & receipts | approve |
| S04 | reimbursement_late_filing | discharge + >window days | warning `late_filing` | reviewer flag (policy rule) |
| S05 | missing_required_doc | drop 1 required doc | docs_pending, needs-info, not rejected | n/a until resubmit |
| S06 | missing_conditional_doc | implant bill line but no sticker | docs_pending (conditional rule) | — |
| S07 | blurry_discharge_summary | blur 2.5 | quality `poor` → re-upload request | — |
| S08 | missing_stamp | no stamp on final bill | stamp flag → needs-info | — |
| S09 | wrong_patient_doc | doc of another member | classification ok, identity mismatch warning | identity mismatch → query |
| S10 | bill_total_mismatch | itemised ≠ final | blocker `totals_mismatch` at builder | — |
| S11 | room_rent_over_cap | tariff > cap | complete | partial, proportional deduction |
| S12 | copay_and_deductible | policy with copay 10% + deductible | complete | partial, calc trace |
| S13 | waiting_period_violation | dx within waiting window | complete | reject / needs reviewer with clause |
| S14 | exclusion_hit | excluded procedure code | complete | reject with clause cite |
| S15 | policy_expired | admission after end date | complete | reject `policy_not_in_force` |
| S16 | sum_insured_exhausted | prior claims consume SI | complete | partial to remaining SI |
| S17 | tampered_amount | `amount_edit` | complete (hospital cannot tell) | authenticity flag → escalate |
| S18 | duplicate_claim | same bill no in two claims | complete | duplicate flag |
| S19 | query_loop_resolved_r2 | tpa-sim asks 2 queries then accepts | answers drafted | resolved after round 2 |
| S20 | query_loop_escalates_r3 | scripted unsatisfactory replies | — | escalation on round 3 |
| S21 | high_value_over_T_four | payable > T_four | — | two approvers required |
| S22 | mid_value_over_T_auto | T_auto < payable ≤ T_four | — | agent asks one human approver |
| S26 | flag_forces_human_below_T_auto | payable ≤ T_auto but `fraud_warning`/failed gate | — | no auto-approve; human decides |
| S23 | handwritten_notes | handwritten=true | low parser confidence → human review field | — |
| S24 | multi_page_bill_50_lines | 6-page itemised bill | pass | pass |
| S25 | pii_stress | fake IDs and phones in free text | masking recall ≥ target | — |

Corpus mix (PROPOSED, 500 cases): 40% clean S01-S03, 35% single-defect, 15% multi-defect, 10% adversarial (S17, S18, S25).

## 7. Determinism and reproducibility
- Global seed → per-case seed `sha256(global_seed|case_index)[:8]`.
- Template versions and generator version in `manifest.json`; re-running with the same versions yields byte-identical PDFs (ReportLab `invariant=1`, fixed creation date `2026-01-01`, except tamper `metadata` cases which set producer on purpose).
- `make seed-data N=500 SEED=42` and `make seed-golden`.

## 8. Loading data into the systems
- `load_hospital.py`: creates cases in hospital-api via its public API using service account (so it also exercises auth) and uploads documents through the upload endpoint (virus scan included).
- `load_insurer.py`: loads policies, members, prior claims, KB docs into insurer-db and rag-service through admin APIs (not SQL) to keep migrations honest. Prior claims create the SI-exhaustion state for S16.
- `replay.py --case S11 --target e2e` runs a case through the full pipeline (uses 05-05 doc).

## 9. Error handling and edge cases
- Generator must refuse to emit if any validation of `01-02` fails for the expected claim (self-check by importing `claim_contract` models and validating `claim_build_fields`).
- Collision: member names via Faker may coincide with real people; acceptable but `id` numbers never are (the `9999` rule). A lint step greps outputs for patterns of valid real Aadhaar (Verhoeff valid and not 9999-prefix) and fails.
- Fonts: bundle open-licence fonts (Noto Sans, Noto Sans Devanagari for bilingual headers on 10% of docs, one script font); no system font dependence.
- Large corpora: generation is parallel by case; 500 cases ≤ 20 min on 4 cores; PDFs ≤ 2 MB each.

## 10. Tests
- Unit: reference calculator vs hand-computed table (30 rows) in `tests/test_reference_calc.py`.
- Property: for any seed, `sum(bill_lines)==totals.gross` unless defect; dates monotone; ICD codes valid.
- Golden hash test (frozen files).
- Label correctness: render → rasterise → crop bbox → compare pixel difference between the label's text render and page (sanity that bbox is where text is; tolerance 3 pt).
- Distribution test: corpus defect mix within ±3% of seed.yaml.

## 11. Acceptance criteria
- `make seed-data N=500` finishes, passes validation, and golden set matches manifest.
- Every archetype S01-S26 has ≥ 10 generated cases (S17/S18 ≥ 15).
- Reference calculator and `calc_engine` agree on ≥ 99% of S01-S16 cases after both are built; remaining diffs documented.
- No generated artefact contains a non-9999 Verhoeff-valid 12-digit number.

## 11A. Detailed build tasks (numbered, with file paths)

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | Create package skeleton, `pyproject` extras `synthetic` (faker, reportlab, pillow, opencv-python-headless, numpy, pikepdf, pyyaml, jsonschema, hypothesis, typer) | `data/synthetic/pyproject.toml`, `data/synthetic/synth/__init__.py` | `uv run synth --help` works |
| 2 | Catalogues (policies, procedures, hospitals) with JSON-Schema validation | `catalog/*.yaml`, `catalog/schema/*.json`, `synth/catalog.py` | `synth validate-catalog` green |
| 3 | RNG and ID utilities (per-case seeding, Verhoeff, fake PAN/IFSC/account) | `synth/rng.py`, `synth/ids.py` | tests in Annex H pass |
| 4 | Entity generators | `synth/gen_entities.py` | uniqueness property test passes |
| 5 | Bill builder + reference calculator | `synth/bill.py`, `synth/reference_calc.py` | 30-row hand table passes |
| 6 | Document templates (ReportLab) + label emission | `synth/templates/*.py`, `synth/labels.py` | label bbox test passes |
| 7 | Stamp / signature drawing | `synth/stamps.py` | vision-service eval can read stamp labels |
| 8 | Degradation | `synth/degrade.py` | quality-class rule test passes |
| 9 | Tamper | `synth/tamper.py` | each tamper has recall-checkable label |
| 10 | Archetype recipes S01-S26 | `synth/archetypes/s01.py … s26.py`, `synth/archetypes/base.py` | each archetype generates 3 valid cases |
| 11 | Expected-output deriver | `synth/expected.py` | `case.json` validates against Annex D schema |
| 12 | KB generator and clause index | `synth/gen_kb.py`, `out/kb/clause_index.json` | clause ids resolvable |
| 13 | Query-script generator for tpa-sim | `synth/queries.py` | script validates against Annex I |
| 14 | Corpus builder, splits, manifests, golden freezer | `synth/corpus.py`, `synth/golden.py` | `make seed-data`, `make seed-golden` pass |
| 15 | Loaders | `synth/load_hospital.py`, `synth/load_insurer.py`, `synth/replay.py` | loads 10 cases through APIs only |
| 16 | Lint: forbidden-number scan, PII-lookalike scan | `synth/lint.py` | CI job `synthetic-lint` |

## 11B. Annex A — `seed.yaml` (full example)

```yaml
schema_version: 1
global_seed: 42
locale: en_IN
corpus:
  total_cases: 500
  splits: {dev: 0.6, test: 0.3, golden_reserved: 0.1}
  mix:                       # must sum to 1.0
    clean: 0.40              # S01, S02, S03
    single_defect: 0.35      # S04-S16, S19-S24
    multi_defect: 0.15       # 2-3 defects combined from the single-defect set
    adversarial: 0.10        # S17, S18, S25
  archetype_min_count: {default: 10, S17: 15, S18: 15, S25: 15}
dates:
  today_anchor: "2026-09-30"        # all dates relative to this; not wall clock (determinism)
  admission_window_days: [20, 400]  # admission between anchor-400d and anchor-20d
  filing_window_days: 30            # reimbursement window default (hospital config doc)
degradation:
  probability_by_archetype: {S07: 1.0, S23: 1.0, default: 0.15}
  blur_sigma: [0.5, 3.0]
  dpi: [72, 150]
  skew_deg: [1, 8]
  jpeg_quality: [25, 70]
tamper:
  amount_edit: {delta_pct: [5, 60]}
  date_edit: {shift_days: [3, 90]}
fonts:
  body: NotoSans-Regular.ttf
  bold: NotoSans-Bold.ttf
  devanagari: NotoSansDevanagari-Regular.ttf
  script: Caveat-Regular.ttf        # handwriting look
  bilingual_header_probability: 0.10
pdf:
  producer: "synth-reportlab/1.0"
  creation_date: "2026-01-01T00:00:00Z"
  invariant: true
limits: {max_pdf_mb: 2, max_pages_per_doc: 8}
```

## 11C. Annex B — Catalogue schemas and examples

### B.1 `policies.yaml`
```yaml
- product_id: PROD-GOLD-5L
  name: "Synthetic Health Gold 5L"
  sum_insured_options: [300000, 500000, 1000000]
  waiting_periods_days: {initial: 30, specific_conditions: 730, pre_existing: 1095}
  specific_conditions: [cataract, hernia, knee_replacement, hysterectomy]
  room_rent_cap_pct_of_si: 1.0       # 1% of SI per day
  icu_cap_pct_of_si: 2.0
  co_pay_pct: 0                      # may be overridden per policy instance
  deductible: 0
  sub_limits:
    - {category: cataract, per_eye: 40000, clause_id: C-5.3}
    - {category: knee_replacement, per_knee: 150000, clause_id: C-5.7}
    - {category: maternity, limit: 75000, clause_id: C-5.9}
  exclusions:
    - {code: COSMETIC, icd_prefix: ["L90","Z41"], clause_id: C-7.1}
    - {code: INFERTILITY, icd_prefix: ["N97"], clause_id: C-7.4}
  proportionate_deduction: true      # if room above cap, all associated charges scaled
  network_only: false
  claim_filing_days: {reimbursement: 30, cashless_intimation_planned_hours: 48, cashless_intimation_emergency_hours: 24}
  clauses: [{id: C-4.2, title: "Room rent", text: "..."}, ...]
```
PROPOSED: policy products 5 (Silver 3L, Gold 5L, Platinum 10L, Senior 5L with 20% co-pay, Corporate floater 5L) so S11, S12, S16 are all representable.

### B.2 `procedures.yaml`
```yaml
- procedure_id: PRC-TKR
  name: "Total knee replacement (unilateral)"
  group: ortho_implant
  icd10_dx: ["M17.1", "M17.0"]
  procedure_codes: ["0SRC0J9"]
  typical_los_days: 4
  los_poisson_lambda: 1.0
  price_range_inr: {total: [180000, 320000], implant: [60000, 140000]}
  implant: true
  required_docs: [prescription, pharmacy_bill, final_bill, procedure_bill, implant_sticker]   # defaults per user decision: prescriptions and bills only; bills carry a hospital stamp; chronological
  waiting_category: knee_replacement
  admission_type_default: planned
```
Minimum 20 procedures: TKR, THR, cataract, lap cholecystectomy, appendectomy, hernia repair, normal delivery, C-section, PTCA/angioplasty, CABG, dengue admission, pneumonia, fracture ORIF, hysterectomy, kidney stone lithotripsy, appendicitis emergency, MI emergency, road-accident polytrauma (MLC), chemo day-care, dialysis session.

### B.3 `hospitals.yaml`
```yaml
- hospital_id: HOSP-001
  name: "Sunrise Multispeciality Hospital"
  city: Pune
  registration_no: "FAKE/MH/2011/00123"
  network: true
  letterhead_style: LH-03
  stamp_style: {shape: circle, ink: blue, text_lines: ["SUNRISE HOSPITAL", "PUNE", "REG FAKE/MH/00123"]}
  doctors: [{name: "Dr. A. Kulkarni", reg_no: "FAKE-MMC-9911", dept: ortho}]
  bill_number_pattern: "SH/{yy}/{seq:06d}"
```
Minimum 12 hospitals, 8 network / 4 non-network, 6 letterhead styles.

## 11D. Annex C — Document label schema (`NN_<doc_type>.labels.json`)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DocumentLabels",
  "type": "object",
  "required": ["schema_version", "doc_id", "doc_type", "page_count", "pages", "fields"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "doc_id": {"type": "string", "pattern": "^[0-9a-f-]{36}$"},
    "doc_type": {"enum": ["discharge_summary","final_bill","itemised_bill","pharmacy_bill","lab_report","radiology_report","investigation_report","admission_note","preauth_approval","claim_form","id_proof","policy_card","cancelled_cheque","implant_sticker","payment_receipt","fir_mlc","other"]},
    "template_id": {"type": "string"},
    "page_count": {"type": "integer", "minimum": 1},
    "pages": {"type": "array", "items": {"type": "object",
      "required": ["page", "width_pt", "height_pt", "stamp_present", "signature_present"],
      "properties": {
        "page": {"type": "integer"}, "width_pt": {"type": "number"}, "height_pt": {"type": "number"},
        "stamp_present": {"type": "boolean"},
        "stamps": {"type": "array", "items": {"$ref": "#/$defs/stamp"}},
        "signature_present": {"type": "boolean"},
        "signatures": {"type": "array", "items": {"$ref": "#/$defs/box"}},
        "handwritten_regions": {"type": "array", "items": {"$ref": "#/$defs/box"}}}}},
    "fields": {"type": "array", "items": {"$ref": "#/$defs/field"}},
    "table": {"type": ["object","null"], "properties": {
      "columns": {"type": "array", "items": {"type": "string"}},
      "rows": {"type": "array", "items": {"type": "object"}},
      "page_subtotals": {"type": "array"}, "grand_total": {"type": "string"}}},
    "degradation": {"type": "object", "properties": {
      "applied": {"type": "array"}, "expected_quality_class": {"enum": ["good","acceptable","poor","unreadable"]},
      "lost_fields": {"type": "array", "items": {"type": "string"}}}},
    "tamper": {"type": ["object","null"], "properties": {
      "type": {"type": "string"}, "bbox": {"$ref": "#/$defs/box"}, "page": {"type": "integer"},
      "original_value": {"type": "string"}, "tampered_value": {"type": "string"}}},
    "pdf_metadata": {"type": "object"}
  },
  "$defs": {
    "box": {"type": "object", "required": ["page","bbox"], "properties": {
      "page": {"type": "integer"}, "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}}},
    "stamp": {"type": "object", "required": ["bbox","shape","text"], "properties": {
      "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
      "shape": {"enum": ["circle","rect","oval"]}, "text": {"type": "string"},
      "rotation_deg": {"type": "number"}, "opacity": {"type": "number"}, "ink": {"type": "string"}}},
    "field": {"type": "object", "required": ["field","value","page","bbox"], "properties": {
      "field": {"type": "string"}, "value": {"type": "string"}, "normalized": {"type": ["string","number","null"]},
      "type": {"enum": ["string","date","money","code","name","phone","id","address","text"]},
      "page": {"type": "integer"}, "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
      "source_text": {"type": "string"}, "is_pii": {"type": "boolean"}, "pii_class": {"enum": ["C2","C3",null]},
      "required_for_claim": {"type": "boolean"}, "lost_after_degradation": {"type": "boolean"}}}
  }
}
```
Field naming convention: dot paths mirroring `claim_build_fields`, e.g. `patient.full_name`, `patient.dob`, `admission.admitted_on`, `bill.total.net_payable`, `bill.line[3].amount`. This lets the eval harness join label → claim builder expectation by key.

Example (abridged) for a `final_bill`:
```json
{"schema_version":"1.0","doc_id":"6d3f…","doc_type":"final_bill","template_id":"FB-02","page_count":1,
 "pages":[{"page":1,"width_pt":595.3,"height_pt":841.9,"stamp_present":true,
   "stamps":[{"bbox":[420,90,540,200],"shape":"circle","text":"SUNRISE HOSPITAL PUNE","rotation_deg":-12,"opacity":0.8,"ink":"blue"}],
   "signature_present":true,"signatures":[{"page":1,"bbox":[400,700,520,740]}]}],
 "fields":[
  {"field":"patient.full_name","value":"Rohan Deshmukh","normalized":"rohan deshmukh","type":"name","page":1,"bbox":[96,210,220,224],"source_text":"Patient Name: Rohan Deshmukh","is_pii":true,"pii_class":"C2","required_for_claim":true},
  {"field":"bill.bill_no","value":"SH/26/001234","type":"code","page":1,"bbox":[400,160,520,174],"is_pii":false},
  {"field":"bill.total.net_payable","value":"₹ 1,84,520.00","normalized":"184520.00","type":"money","page":1,"bbox":[430,600,540,616],"required_for_claim":true}
 ],
 "degradation":{"applied":[],"expected_quality_class":"good"}}
```

## 11E. Annex D — `case.json` JSON Schema (key parts)

```json
{
  "title": "SyntheticCaseTruth",
  "type": "object",
  "required": ["schema_version","case_id","seed","archetype","claim_type","admission_type","member","policy","hospital","admission","bill_lines","totals","documents","defects_applied","expected"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "case_id": {"pattern": "^SYN-[0-9]{6}$"},
    "seed": {"type": "integer"},
    "archetype": {"pattern": "^(S(0[1-9]|1[0-9]|2[0-5]))$|^multi:.*$"},
    "archetype_name": {"type": "string"},
    "claim_type": {"enum": ["cashless","reimbursement"]},
    "admission_type": {"enum": ["planned","emergency"]},
    "split": {"enum": ["dev","test","golden"]},
    "member": {"$ref": "#/$defs/member"},
    "policy": {"$ref": "#/$defs/policy"},
    "hospital": {"$ref": "#/$defs/hospital"},
    "prior_claims": {"type": "array", "items": {"$ref": "#/$defs/prior_claim"}},
    "admission": {"$ref": "contract:Admission"},
    "bill_lines": {"type": "array", "items": {"$ref": "contract:BillLine"}},
    "totals": {"$ref": "contract:ClaimTotals"},
    "documents": {"type": "array", "items": {"$ref": "#/$defs/document"}},
    "defects_applied": {"type": "array", "items": {"type": "string", "pattern": "^[a-z_]+(:[A-Za-z0-9_.-]+)?$"}},
    "expected": {"$ref": "#/$defs/expected"}
  },
  "$defs": {
    "member": {"required": ["member_id","full_name","dob","gender","id_proof_type","id_number_synthetic","id_proof_hash"]},
    "policy": {"required": ["policy_number","product_id","start","end","sum_insured","co_pay_pct","deductible","room_rent_cap_per_day"]},
    "prior_claim": {"required": ["claim_ref","paid_amount","admitted_on","diagnosis_codes","bill_no"]},
    "document": {"required": ["doc_id","doc_type","file","pages","quality_class","defects","fields"]},
    "expected": {"required": ["hospital","insurer"]}
  }
}
```
`contract:X` resolves to the JSON Schema exported by `claim_contract` (see `01-shared-contract/02-data-models-and-enums.md` task 2). Validation script: `synth validate-truth out/` fails on any violation.

### D.1 Expected schemas (`expected/hospital.json`, `expected/insurer.json`)

`expected/hospital.json`
```json
{
  "classification": [{"doc_id": "…", "doc_type": "discharge_summary", "allow_alt": []}],
  "quality": [{"doc_id": "…", "class": "acceptable", "blocking": false}],
  "stamp": [{"doc_id": "…", "page": 1, "present": true, "required": true}],
  "completeness": {"complete": true, "missing": [], "conditional_triggers": [], "blockers": [], "warnings": []},
  "router": {"claim_type": "cashless", "admission_type": "planned", "procedure_group": "ortho_implant", "flags": []},
  "claim_build_fields": {"patient.full_name": "Rohan Deshmukh", "admission.admitted_on": "2026-03-04"},
  "expected_case_status": "ready_for_review",
  "expected_needs_info": null
}
```
`expected/insurer.json`
```json
{
  "identity": {"match": true, "score_min": 0.90, "mismatches": []},
  "authenticity": {"flag": false, "reason_codes": [], "tamper_ref": null},
  "coverage": {"in_force": true, "waiting_period_ok": true, "exclusions_hit": [], "sub_limit_hits": [], "clauses": ["C-4.2"]},
  "calculation": {"claimed": "180000.00", "eligible_before_rules": "176500.00", "deductions": [], "payable": "142000.00", "rule_order": ["exclusions","sub_limits","room_rent_proportion","deductible","copay","sum_insured_cap"]},
  "decision": {"outcome": "partial", "route": "reviewer", "needs_one_approver": true, "needs_two_approvers": false, "reason_codes": ["room_rent_cap"]},
  "queries": [],
  "expected_insurer_status": "awaiting_approval"
}
```

## 11F. Annex E — Generator code outline (signatures and key algorithms)

```python
# synth/rng.py
def case_seed(global_seed: int, idx: int) -> int:
    h = hashlib.sha256(f"{global_seed}|{idx}".encode()).digest()
    return int.from_bytes(h[:8], "big")


class Rng:
    def __init__(self, seed):
        self.r = random.Random(seed)
        self.fake = Faker("en_IN")
        self.fake.seed_instance(seed)

    def choice(self, seq):
        return self.r.choice(seq)

    def poisson(self, lam): ...  # Knuth algorithm using self.r (do not use numpy global state)
    def money(self, lo, hi, step=10): ...  # Decimal multiple of step
```
```python
# synth/ids.py
VERHOEFF_D = [...]
VERHOEFF_P = [...]
VERHOEFF_INV = [...]  # standard tables


def verhoeff_check_digit(num: str) -> str: ...
def fake_aadhaar(rng) -> str:
    body = "9999" + "".join(str(rng.r.randint(0, 9)) for _ in range(7))  # 11 digits
    return body + verhoeff_check_digit(body)  # 12 digits, Verhoeff-valid


def fake_pan(rng) -> str:
    return "ZZZZZ" + f"{rng.r.randint(0, 9999):04d}" + "Z"  # ZZZZZ prefix never issued (PROPOSED)


def fake_ifsc(rng) -> str:
    return "FAKE0" + f"{rng.r.randint(0, 999999):06d}"


def fake_account(rng) -> str:
    return "000" + "".join(str(rng.r.randint(0, 9)) for _ in range(11))


def id_proof_hash(raw: str, salt: str) -> str:
    return hashlib.sha256((salt + normalise_id(raw)).encode()).hexdigest()
```
```python
# synth/archetypes/base.py
@dataclass
class Recipe:
    archetype: str
    claim_type: ClaimType | None  # None = random per recipe
    admission_type: AdmissionType | None
    procedure_filter: Callable[[Procedure], bool]
    policy_filter: Callable[[PolicyProduct], bool]
    defects: list[Defect]  # ordered; each has .apply(case, rng)
    expect: Callable[[Case], Expected]  # derives expected from final case state (after defects)


class Defect(Protocol):
    name: str

    def apply(
        self, case: Case, rng: Rng
    ) -> None: ...  # mutates case; must record in case.defects_applied
```
```python
# synth/corpus.py
def build_case(idx, cfg, catalogs, recipe, out_dir) -> Case:
    rng = Rng(case_seed(cfg.global_seed, idx))
    base = make_entities(rng, catalogs, recipe)
    case = assemble_case(rng, base, recipe)  # dates, bill lines, documents spec
    for d in recipe.defects:
        d.apply(case, rng)
    render_documents(case, rng, out_dir)  # templates → PDFs + labels (clean)
    apply_degradations(case, rng, out_dir)  # raster → degraded PDFs + label updates
    apply_tampers(case, rng, out_dir)
    case.expected = recipe.expect(case)  # reference calc, completeness rules, thresholds
    validate_case(case)  # claim_contract + JSON schemas
    write_case(case, out_dir)
    write_manifest(case, out_dir)
    return case


def build_corpus(cfg):
    plan = allocate_archetypes(cfg)  # honour mix and archetype_min_count
    with ProcessPoolExecutor(cfg.workers) as ex:
        ...  # one case per task; sort results by idx for determinism
    write_corpus_manifest()
    write_splits()
```
Order matters: defects that change the bill must run before rendering; degradation/tamper run after rendering because they operate on pixels/PDF objects; the expected deriver runs last and reads final state, so tamper/defect effects are always reflected.

### E.1 Bill line generation algorithm
1. Choose total target `T` from procedure `price_range_inr.total` (log-uniform).
2. Allocate by category fractions (PROPOSED defaults): room 12-20%, ICU 0-15%, surgeon+anaesthesia+OT 20-30%, implants 0-45% (if `implant`), medicines 8-15%, consumables 4-8%, investigations 4-8%, consultations 2-4%.
3. For each category produce lines: room = days × tariff by class (general 2500, semi 4000, private 6500, deluxe 9500, ICU 12000 INR); medicines 10-60 lines drawn from `medicines.yaml` with qty and MRP, GST-inclusive; investigations from `investigations.yaml`.
4. Scale last line of each category so the category sum equals the allocation (cents-exact), then round all unit prices to 2dp half-up and recompute `amount = qty*unit_price`; finally adjust a "round-off" line (−0.99..+0.99) so grand total matches `T_rounded`.
5. Discounts: optional hospital discount 0-5% as negative line `category=other`.
6. Emit page-wise subtotals in `itemised_bill` (max 14 lines/page for layout 1, 22 for layout 3).

## 11G. Annex F — The 26 scenarios in detail

Common assumptions: policy in force unless stated; identity matches; documents clean unless stated; thresholds `T_auto=50,000`, `T_four=500,000`. "Hospital expect" = `expected/hospital.json`; "Insurer expect" = `expected/insurer.json`. Counts are minimum per corpus.

### S01 clean_cashless_planned (min 60)
- Recipe: claim_type=cashless, admission planned, any non-excluded procedure, policy product any with SI ≥ 2× bill, no prior claims, all docs from `required_docs`, preauth_approval present with approved amount ≥ claimed.
- Hospital: classification all correct; completeness complete; router correct; status `ready_for_review`.
- Insurer: identity match; authenticity false; coverage all true; payable = claimed − non-payables (typically consumables per policy list, ≤ 3% of bill); route by amount: all gates pass and ≤T_auto → system auto-approves (`decision.auto_approved`); else a human approver decides.
- Checks: no queries; time-to-ready measured.

### S02 clean_cashless_emergency (min 40)
- Recipe: emergency admission (MI, appendicitis, polytrauma), preauth_approval **optional** (emergency intimation within 24h flag in claim_form), FIR absent unless polytrauma accident (then `fir_mlc` required).
- Hospital: complete; flags `emergency`; deadlines config used = emergency set.
- Insurer: approve path; coverage in_force; waiting-period exempt for accident per clause C-6.2 (when dx is accident-coded S/T codes).
- Edge: initial 30-day waiting period waived for emergencies due to accidents but NOT for illness; include 10 cases of each.

### S03 clean_reimbursement_planned (min 40)
- Recipe: reimbursement; adds `payment_receipt` and `cancelled_cheque`; `claim_form` carries bank details matching cheque.
- Hospital: complete; ready; filing deadline computed from discharge (config `deadlines.reimbursement_filing_days`).
- Insurer: approve with payable; bank name/IFSC cross-check passes (authenticity sub-check: cheque holder = patient/claimant).

### S04 reimbursement_late_filing (min 10)
- Defect `late_filing`: submit date = discharge + (filing_window + U[1,45]) days.
- Hospital: `warnings:["late_filing"]`; still submittable by officer with reason (not blocked).
- Insurer: reason code `late_filing`; route reviewer (never auto); expected outcome = `needs_info` for condonation reason (PROPOSED) — query category `other`.

### S05 missing_required_doc (min 20)
- Defect `missing_doc:<type>` drop exactly one required doc, rotating across: prescription, pharmacy_bill, final_bill, procedure_bill (when the case has surgery).
- Hospital: `docs_pending`, `missing:[<type>]`, a needs-info request is created (never rejection; principle 7); reminder scheduled per deadlines config; no submission allowed.
- After "re-upload" step (script adds the file): completeness flips to complete (tests the loop).

### S06 missing_conditional_doc (min 15)
- Defect: bill contains line `category=implant` but `implant_sticker` removed; or `fir_mlc` removed on accident dx.
- Hospital: conditional rule triggers (`conditional_triggers:["implant_line→implant_sticker"]`), `docs_pending`.
- Negative control: same recipe without implant line must be complete (ensures rule not over-firing).

### S07 blurry_discharge_summary (min 15)
- Defect `blur:2.5`, dpi 100.
- Hospital: quality class `poor`; blocking for discharge_summary (required, field-bearing) → re-upload request with reason `illegible_document`; claim builder not run.
- Variant S07b: blur 1.2 → `acceptable`, parses with lower confidence; must NOT block.

### S08 missing_stamp (min 15)
- Defect `missing_stamp` on final_bill (and variant `stamp_faint` opacity 0.2).
- Hospital: vision stamp detection false → blocker `stamp_missing` (needs-info "get stamped copy"), not rejection.
- Variant check: stamp present on other docs; only final_bill flagged.

### S09 wrong_patient_doc (min 15)
- Defect `foreign_doc:<type>`: replace one doc (e.g. lab_report) with one generated for another member of same hospital.
- Hospital: classification OK; cross-document consistency check (name/dob across docs) yields warning `patient_mismatch` (not blocker, officer decides).
- Insurer: identity agent reports mismatch (`mismatches:["lab_report.patient.full_name"]`), query category `identity_mismatch`.

### S10 bill_total_mismatch (min 15)
- Defect `bill_total_mismatch`: itemised sum ≠ final bill total by delta in {0.01, 10, 100, 1% , 5%}.
- Hospital: claim builder blocker `totals_mismatch` (contract 422 semantics) → officer must fix/override with note; delta 0.01 considered rounding → warning only (tolerance ₹1 PROPOSED).
- Truth: expected blockers list contains `totals_mismatch` except delta ≤ 1.00.

### S11 room_rent_over_cap (min 20)
- Recipe: policy with room_rent_cap 1% of SI/day, room tariff above cap by ratio r in {1.1, 1.5, 2.0}.
- Insurer calc (proportionate deduction ON): eligible = sum(room_lines) at cap; every "associated" line (doctor fees, OT, nursing) scaled by cap/actual; medicines/implants excluded from scaling per clause C-4.2(b).
- Worked example in Annex G.1.

### S12 copay_and_deductible (min 20)
- Policy `Senior 5L` co_pay 20% plus per-claim deductible ₹5,000 (variant: no deductible).
- Calc order: eligible → deductible → co-pay (see Annex G for order rule). Expect `payable = (eligible − deductible) × (1 − copay)`.

### S13 waiting_period_violation (min 15)
- Dx in `specific_conditions` with admission < policy start + 730 days (and initial 30-day period variant, pre-existing 1095-day variant).
- Insurer: `waiting_period_ok=false`, clause cite (C-6.1/6.3), outcome `reject` recommendation but route **human** (never auto-reject); reviewer must confirm.

### S14 exclusion_hit (min 15)
- Procedure in exclusions (cosmetic, infertility); mixed variant: one excluded line inside an otherwise payable bill (partial).
- Expect `exclusions_hit:["COSMETIC"]`, clause C-7.1, deduction rule `exclusion`.

### S15 policy_expired (min 10)
- Admission date > policy end by 1-60 days; boundary variant: admission exactly on end date (in force) and day after (not).
- Expect `in_force` false/true accordingly; reject with `policy_not_in_force`.

### S16 sum_insured_exhausted (min 15)
- Prior claims paid total P so remaining SI R = SI − P; bill > R. Variants R=0, R tiny.
- Calc applies SI cap last: payable = min(computed, R). `prior_claims` loaded by `load_insurer.py`.

### S17 tampered_amount (min 15)
- Defect `tamper:amount_edit` on final_bill or pharmacy_bill; also variants `date_edit`, `name_swap`, `stamp_reuse`, `template_mismatch`, `metadata`, `arith` (rotate; min 3 each).
- Hospital cannot detect (expected: complete) except `arith` which hospital builder also catches (document internal arithmetic).
- Insurer: authenticity flag true with reason code from the tamper table; route escalate to approver with fraud flag; outcome `needs_info` or hold (PROPOSED: hold + human).

### S18 duplicate_claim (min 15)
- Two cases share `bill.bill_no` and hospital, second submitted later (or same member different policy).
- Insurer: duplicate flag on second; reason `duplicate_bill_no`; never auto-pay.

### S19 query_loop_resolved_r2 (min 15)
- `expected/queries.json` script: round 1 query category `billing_discrepancy` asking for itemisation of 3 lines; round 2 `medical_clarification` asking indication; tpa-sim accepts after round-2 response contains required keys (`doc_type: investigation_report` attached AND text mentions diagnosis code).
- Expect: Query Responder drafts grounded answer each round; human edits count recorded; case resolves after round 2; status returns to `verifying`.

### S20 query_loop_escalates_r3 (min 10)
- Script: tpa-sim rejects answers in rounds 1, 2 as unsatisfactory (`reject_reason` set) and raises round 3.
- Expect: round 3 → insurer `escalated`, hospital `under_query` with escalation banner; no round 4 created; human decision required.

### S21 high_value_over_T_four (min 10)
- Payable > 500,000 (SI 10L policy, procedures CABG, complex ortho).
- Expect `needs_two_approvers=true`; approvers distinct and distinct from reviewer (separation-of-duties test).

### S26 flag_forces_human_below_T_auto (min 15)
- Payable ≤ 50,000 but one hard gate fails or a `review_required` flag is set (`fraud_warning`, `degraded_mode`, `watchlist_hospital`): expect route `one_approver`, never auto-approve.

### S22 mid_value_over_T_auto (min 15)
- 50,000 < payable ≤ 500,000: the agent asks one human approver. Boundary cases at exactly 50,000.00 (treated as ≤ T_auto) and 50,000.01.

### S23 handwritten_notes (min 15)
- Defect `handwritten`: doctor's discharge notes and admission note rendered with script font plus noise.
- Expect: parser confidence below gate on those fields → field flagged for human entry (`needs_human_field`), not auto-filled; other fields normal.

### S24 multi_page_bill_50_lines (min 10)
- Itemised bill 40-80 lines over 3-6 pages with page subtotals and a carry-forward header; also variant with table split across page boundary and repeated header rows.
- Expect: line recall ≥ target; totals reconcile.

### S25 pii_stress (min 15)
- Free-text sections (discharge notes, claim form remarks) with injected PII: names (incl. relatives and doctors), phones in 4 formats, 9999-prefix Aadhaar in 3 groupings, PAN-like, account numbers, IFSC, emails, addresses with PIN, DOB in 3 formats, UHID/IP no.
- Labels: `pii_spans[]` per doc: `{start,end,class,text}` on the text layer (and bbox for images).
- Expect: masking recall/precision metrics; zero C3 string in egress capture.

### Multi-defect combinations (15% of corpus)
Combination matrix (examples): S05+S07, S10+S11, S12+S16, S13+S17, S08+S19. Rule: at most one hospital-blocking defect plus up to two insurer-side defects; the expected deriver uses first-blocker semantics (hospital blockers stop the pipeline; insurer expectations recorded as "post-fix" expectations).

## 11H. Annex G — Reference calculator (independent implementation)

Order of operations (PROPOSED, mirrors `calc-engine` doc; any divergence is a doc bug to resolve jointly):
1. Start with claimed bill lines.
2. Remove **non-payable items** list (consumables list in policy) and **exclusions** by ICD/proc code → deduction rule ids `non_payable`, `exclusion`.
3. Apply **sub-limits** per category (min(amount, limit)) → `sub_limit`.
4. Apply **room-rent proportional deduction** → `room_rent_cap`.
5. Apply **deductible** (per claim) → `deductible`.
6. Apply **co-pay %** on the remaining → `copay`.
7. Cap at **remaining sum insured** → `si_cap`.
8. Round half-up to 2 dp at the end of each rule (rounding trace stored).

### G.1 Worked example — S11
Policy SI 500,000; room cap 1% = 5,000/day; stay 4 days; room tariff 7,500/day (ratio 1.5).
- Room charges claimed 30,000; eligible 4×5,000 = 20,000; deduction 10,000.
- Proportion factor f = 5,000/7,500 = 0.6667 applied to associated charges: surgeon fee 40,000 → 26,666.67 (deduct 13,333.33); OT 25,000 → 16,666.67 (8,333.33); nursing 8,000 → 5,333.33 (2,666.67).
- Medicines 30,000, implants 80,000, investigations 12,000: not scaled.
- Gross claimed 225,000; deductions 10,000 + 13,333.33 + 8,333.33 + 2,666.67 = 34,333.33; payable 190,666.67 (before copay 0, deductible 0).

### G.2 Worked example — S12
Eligible after steps 2-4 = 120,000; deductible 5,000; co-pay 20%.
- After deductible 115,000; co-pay 23,000; payable 92,000.
- Trace: `deductible 5000.00`, `copay 23000.00`.
Alt-order trap (do not use): co-pay then deductible → 0.8×120,000 − 5,000 = 91,000; documenting order prevents silent drift.

### G.3 Worked example — S16
SI 300,000; prior paid 260,000 → R=40,000; computed payable 95,000 → `si_cap` deduction 55,000; payable 40,000.

### G.4 Rounding rule
Decimal with `ROUND_HALF_UP` to 0.01 at each rule application; proportional factors kept as `Decimal` with 10 dp internally, not rounded until line amounts.

## 11I. Annex H — Verhoeff and lint rules

Tests (`tests/test_ids.py`): known vector (payload `236` → check digit `3`, so `2363` validates; `2364` does not), generated Aadhaar-like pass `verhoeff_valid`, all begin `9999`; mutate any digit → fails validation with probability 1 for single-digit errors and adjacent transpositions (Verhoeff property).
Lint (`synth lint`): scan every text artefact (`case.json`, labels, extracted PDF text layers, KB) with regex `\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b`; any Verhoeff-valid match not starting `9999` → fail with file/line. PAN-like scan `\b[A-Z]{5}\d{4}[A-Z]\b` must start `ZZZZZ`. Phones: `\b[6-9]\d{9}\b` allowed only in ranges `98000 00000-98000 99999` reserved fake block (PROPOSED).

## 11J. Annex I — tpa-sim query script schema (`expected/queries.json`)

```json
{
  "script_version": "1.0",
  "case_id": "SYN-000412",
  "rounds": [
    {"round": 1, "category": "billing_discrepancy",
     "text_template": "Please provide itemised breakup for lines {line_refs}.",
     "params": {"line_refs": [12, 13, 14]},
     "requested_doc_types": ["itemised_bill"],
     "satisfied_when": {"all": [{"attached_doc_type": "itemised_bill"}, {"text_contains_any": ["itemised", "breakup"]}]},
     "on_unsatisfied": {"next_round": 2, "reason": "Incomplete breakup"}},
    {"round": 2, "category": "medical_clarification",
     "text_template": "Clarify indication for {procedure_name}.",
     "satisfied_when": {"all": [{"text_contains_icd": true}, {"attached_doc_type": "investigation_report"}]}}
  ],
  "terminal": {"if_satisfied_round_leq": 2, "resolution": "back_to_verifying", "else": "escalate_round_3"},
  "latency_s": {"min": 2, "max": 10}
}
```
tpa-sim evaluates `satisfied_when` deterministically (no LLM), making resolution reproducible.

## 11K. Additional edge cases and tests

- Leap-day and month-end admission dates; discharge same day as admission (day-care) → LOS 0 → room line absent for day-care procedures.
- Names with titles, initials, mononyms, long names (>40 chars wrapping), Devanagari header bilingual docs; PDFs with rotated pages (90°) flagged in labels.
- Currency formatting variants: `1,84,520.00` (Indian grouping), `184520`, `₹184,520.00`; labels hold both `value` and `normalized`.
- Determinism test: generate case 17 twice with 4 workers vs 1 worker → identical sha256 for all files.
- Corpus statistics report (`synth stats`) prints per-archetype counts, bill totals distribution, quality-class counts; CI compares against seed targets.
- Memory: rasterisation at 200 dpi limited to one page at a time to stay under 1 GB per worker.

## 12. Dependencies and Claude Code kickoff prompt
Depends on: `01-shared-contract/02-data-models-and-enums.md` (models), `03-config-versioning.md` (policy rule payload shape), `01-api-contract-v1.md` (loaders). Consumed by: 02-evaluation-harness, all agent docs' test fixtures, tpa-sim.

> Implement docs/implementation/05-integration-and-eval/01-synthetic-data.md sections 4.1-4.4 first (entities, cases, documents, degradation) with the reference calculator and the S01-S12 archetypes; then 4.5 tamper and S13-S25. Use ReportLab, Faker en_IN, seeded RNG. Produce golden set manifest. Run all tests in section 10 before stopping.
