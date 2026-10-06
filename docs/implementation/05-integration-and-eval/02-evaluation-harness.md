# 05-02 — Evaluation Harness

Status: PROPOSED. Joint ownership. Code in `eval/`. Consumes ground truth from `05-01-synthetic-data.md`.

## 1. Goal
Measure every automated stage against synthetic ground truth, per component and end-to-end, produce a reproducible report, and gate regressions in CI. Principle 5 (evidence over confidence): the harness also tests that gates are well calibrated, i.e. that parser confidence and two-pass agreement actually predict correctness.

## 2. Inputs / Outputs
Inputs: corpus (`data/synthetic/out`), running stack (or component mocks), `eval/config.yaml` (which suites, model aliases, thresholds), previous run results for diffing.
Outputs per run in `eval/runs/<timestamp>_<gitsha>/`:
```
run.json            # config, versions (config versions, prompt versions, model aliases, commit)
results.parquet     # one row per (case, component, metric-unit) with pred, truth, correct
metrics.json        # aggregated metrics with CIs
report.md / report.html
failures/<component>/<case>.json   # inputs, outputs, diff, Langfuse trace id
calibration/*.png
```

## 3. Data model
`results` rows: `run_id, case_id, archetype, component, unit_id (doc_id|field|line_ref), metric_name, pred, truth, correct(bool), score(float|null), latency_ms, tokens_in, tokens_out, cost_usd, trace_id, error(text|null)`.
SQLite/Parquet only; no production DB access. PROPOSED: DuckDB for querying parquet.

## 4. Suites and metrics

### 4.1 Document pipeline (doc-pipeline, vision-service) — Dev A components
| Metric | Definition | Target (PROPOSED) |
|---|---|---|
| Parse field accuracy | exact normalised match on labelled fields (dates ISO, money Decimal, names casefold and whitespace collapse) | ≥ 95% clean, ≥ 85% degraded-acceptable |
| Character error rate (CER) | edit distance / ref length on label `source_text` | ≤ 3% clean |
| Table line recall / precision | bill lines matched by (description fuzzy ≥ 0.85, amount exact) | ≥ 95% / ≥ 95% |
| Amount exact match | each bill line amount and total | ≥ 99% |
| Classification accuracy | `DocType` per document; macro-F1 | acc ≥ 97%, macro-F1 ≥ 0.95 |
| Quality class accuracy | predicted vs `expected_quality_class`; confusion matrix; recall on `unreadable` | recall ≥ 0.95 |
| Stamp detection | per-page precision/recall, IoU ≥ 0.5 | P ≥ 0.9, R ≥ 0.9 |
| Stamp OCR | CER on stamp text | ≤ 15% |
| Signature presence | P/R | ≥ 0.85 |
| PII masking | recall on injected PII spans (names, phone, ID, address, email), precision on non-PII | recall ≥ 0.98 ID-like / ≥ 0.95 names; precision ≥ 0.9 |
| Latency | p50/p95 per page | p95 ≤ 20 s / page on dev CPU |

### 4.2 Hospital completeness / router / claim builder (Dev A)
| Metric | Definition | Target |
|---|---|---|
| Completeness verdict accuracy | `complete` flag equals truth | 100% (deterministic code) — any miss is a bug |
| Missing-doc set F1 | predicted vs expected missing types | 1.0 |
| Router accuracy | claim_type, admission_type, procedure_group | 100% / 100% / ≥ 98% |
| Claim builder field accuracy | each `claim_build_fields` path | ≥ 97% |
| Builder validation pass rate | fraction producing a valid `ClaimSubmission` on clean cases | ≥ 98% |
| Blocker detection | S10 totals_mismatch, S08 stamp | 100% recall |
| False-complete rate | cases declared complete that truth says incomplete | 0 |

### 4.3 Insurer verification (Dev B)
| Metric | Definition | Target |
|---|---|---|
| Identity match | accuracy for match/mismatch; ROC for score | acc ≥ 98%; mismatch recall ≥ 0.95 |
| Authenticity flag | per tamper type recall; false-positive rate on clean | recall ≥ 0.85 (amount_edit ≥ 0.8, arith 1.0, metadata 1.0, duplicate_bill_no 1.0); FPR ≤ 3% |
| Coverage determination | in_force, waiting_period_ok, exclusions_hit set accuracy | ≥ 99% |
| Clause citation | cited clause ids ⊇ truth clause ids; precision | recall ≥ 0.9, precision ≥ 0.8 |
| Calculation agreement | calc_engine vs reference_calc payable exact match | ≥ 99.5% on S01-S16; disagreement list must be empty after triage |
| Calculation mapper accuracy | mapped bill line → policy category | ≥ 97% |
| Decision route correctness | thresholds → route (auto-approve/one approver/two approvers) | 100% |
| Wrongful auto path | cases auto-approved although payable > T_auto or a gate/flag failed | 0 |
| False auto-approve rate | auto-approved cases whose ground-truth outcome is not approve/partial-as-calculated (incl. S13-S18 defects) ÷ all auto-approved cases | < 1% (used to re-tune `T_auto` and gate scores) |
| Auto-approve coverage | auto-approved ÷ clean cases ≤ T_auto | reported (target ≥ 80%) |

### 4.4 Query loop
| Metric | Definition | Target |
|---|---|---|
| Triage category accuracy | incoming scripted query → `QueryCategory` | ≥ 90% |
| Draft grounding | % claims in draft supported by case data or KB passage (LLM-judge + rule check for numbers/dates) | ≥ 95% numbers exact; hallucinated-fact rate ≤ 2% |
| Draft acceptance by tpa-sim | scripted satisfied-condition met | ≥ 85% resolve by round 2 |
| Escalation correctness | S20 escalates exactly on round 3 | 100% |
| Round limit | no 4th round ever | 100% |

### 4.5 RAG
Recall@k (k=5) on question → clause ids; MRR; faithfulness (answer spans supported by retrieved text); citation precision. Targets recall@5 ≥ 0.9, MRR ≥ 0.75. Questions generated from clause templates in `gen_kb.py` plus 50 hand-written paraphrases.

### 4.6 End-to-end
Per archetype: final status equals expected; time-to-approval-ready (discharge doc upload → `ready_for_review`), wall clock and active compute; number of human touches; number of agent errors; total LLM cost and tokens per claim. Target (PROPOSED): median upload→`ready_for_review` ≤ 10 min on dev machine with free-tier API; ≤ 3 min for ≤ 20 page cases when run with a warmed stack.

### 4.7 Calibration (principle 5)
For each gated signal (parser confidence, two-pass agreement, vision confidence): reliability diagram, Expected Calibration Error, and **gate effectiveness**: at the configured threshold, precision of "auto-pass" (fraction correct among passed) and coverage (fraction passed). Required: auto-pass precision ≥ 0.98 for fields feeding money values; otherwise raise threshold or send to human. LLM self-reported confidence is recorded but never used; the report states its correlation with correctness to justify that decision.

## 5. Harness design

### 5.1 Layout
```
eval/
  cli.py                  # typer: run, compare, report, calibrate
  suites/
    doc_pipeline.py  completeness.py  claim_builder.py  identity.py
    authenticity.py  coverage.py  calculation.py  query.py  rag.py  e2e.py
  matchers.py             # normalise, fuzzy, money, date, set F1
  judge.py                # LLM-as-judge wrapper (through llm-gateway alias `judge`)
  clients/                # thin API clients using service accounts
  report/                 # jinja templates, plots (matplotlib)
  config.yaml
```

### 5.2 Execution modes
- `component` mode: calls a service API directly with ground-truth inputs (isolates the component from upstream errors). E.g. completeness engine fed with true classifications.
- `cascade` mode: uses the previous stage's actual outputs (measures compounded error).
- `e2e` mode: drives real UIs' APIs from upload to decision, with tpa-sim scripted.
Report both component and cascade numbers; the delta quantifies error propagation.

### 5.3 Matchers
- Money: `Decimal` equality after stripping currency symbols and commas; tolerance 0 for payable, ±0.01 for OCR'd lines (reported separately).
- Dates: parse with explicit formats `dd/mm/yyyy, dd-mm-yyyy, d MMM yyyy`; ISO compare.
- Names: casefold, strip titles (Mr, Mrs, Dr, Smt), collapse whitespace; token-set ratio ≥ 0.95 counts correct; report strict as well.
- Lists: set precision/recall/F1.
- Boxes: IoU.
- Text: CER via `rapidfuzz.distance.Levenshtein`.

### 5.4 LLM-as-judge (only for free text)
Judge alias `judge` should differ from the model under test where possible (avoid self-preference). Judge prompt versioned in Langfuse; rubric returns JSON `{supported: bool, unsupported_spans: [...]}`. Calibrate judge on 40 human-labelled drafts and report agreement (Cohen's kappa ≥ 0.7); if lower, rely on rule checks only for gating. Temperature 0. Cache by hash.

### 5.5 Statistics
Report point estimate plus 95% bootstrap CI (1000 resamples) per metric and per archetype; paired bootstrap for run comparison. Do not claim improvement when CIs overlap. Minimum n per archetype 10; metrics with n<30 flagged "low-n".

### 5.6 Regression gating (CI)
- Fast suite (`make eval-fast`): a small golden subset (size chosen by Claude Code; no fixed count), component mode, mocked LLM replay (VCR-style recorded responses keyed by prompt hash) so no network. Fails PR if any deterministic metric < target or any non-LLM metric regresses.
- Nightly full suite on corpus (needs LLM quota; skipped gracefully with report `QUOTA_LIMITED` if gateway returns 429s).
- Thresholds live in `eval/thresholds.yaml`, changed only via PR approved by both devs.

## 6. Pseudocode: component run
```python
def run_suite(suite, cases, client, cfg):
    rows = []
    for case in cases:  # parallel with semaphore(cfg.concurrency)
        truth = load_truth(case, suite.truth_path)
        t0 = now()
        try:
            pred, meta = suite.invoke(client, case)  # meta has trace_id, tokens, cost
        except Exception as e:
            rows.append(error_row(case, suite, e))
            continue
        for unit in suite.units(truth, pred):
            rows.append(
                Row(
                    case.id,
                    suite.name,
                    unit.id,
                    unit.metric,
                    unit.pred,
                    unit.truth,
                    unit.correct,
                    unit.score,
                    now() - t0,
                    meta.tokens_in,
                    meta.tokens_out,
                    meta.cost,
                    meta.trace_id,
                )
            )
    return rows
```
Errors count as incorrect (never silently dropped) and are reported as `error_rate`.

## 7. Config / env vars
`EVAL_STACK_URLS` (hospital/insurer/gateway endpoints), `EVAL_SERVICE_TOKEN_HOSP`, `EVAL_SERVICE_TOKEN_INS` (Keycloak client credentials), `EVAL_JUDGE_ALIAS=judge`, `EVAL_CONCURRENCY=2` (free-tier friendly), `EVAL_REPLAY_DIR=eval/replay`, `EVAL_BUDGET_USD=0` (hard stop on any spend; PROPOSED).

## 8. Error handling and edge cases
- Rate limits: exponential backoff honoring `Retry-After`; if budget of retries exhausted mark rows `error=quota`. Report shows percent quota-limited and excludes them from accuracy but lists them.
- Non-determinism: LLM stages run 3 times on golden set (temperature per production setting); report mean and variance, plus **flip rate** (cases whose correctness changes between runs); target flip rate ≤ 5%.
- Stack failure mid-run: harness checkpoints per case, resumable `--resume`.
- Data leakage: eval never writes to production-like DBs except through APIs and into synthetic namespaces; `case_id` prefix `SYN-` enforced; refuse to run if API reports non-synthetic tenant flag.
- Truth bugs: when a failure is suspected to be a truth error, the report has a "suspected truth defects" section (high-confidence disagreement across components) to triage rather than silently pass.

## 9. Tests (for the harness itself)
Matchers unit tests (Claude Code generates the cases itself; no fixed count), bootstrap CI against known distributions, judge cache, resume logic, report golden-file test with a fabricated results parquet, a "sabotage test" which injects a deliberately broken mock service and asserts the gate fails.

## 10. Report structure
1. Summary table by component (metric, value, CI, target, pass/fail). 2. Archetype heatmap (component × archetype accuracy). 3. Cascade vs component delta. 4. Calibration plots. 5. Cost/latency table (p50/p95, tokens, USD or free-tier calls). 6. Top 20 failures with trace links. 7. Run-to-run diff. 8. Human-in-the-loop load: human touches per claim.

## 11. Acceptance criteria
- `make eval-fast` runs offline in ≤ 10 min and fails on sabotage.
- Full run produces report with all sections and CIs; calibration shows gate effectiveness for each gated signal.
- All "100%" targets in 4.2-4.4 hold or have a filed defect each.

## 11A. Detailed build tasks (numbered, with file paths)

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | Package skeleton, deps (typer, pandas, pyarrow, duckdb, rapidfuzz, numpy, scipy, matplotlib, jinja2, httpx, tenacity, pydantic, pytest) | `eval/pyproject.toml`, `eval/evalkit/__init__.py` | `eval --help` |
| 2 | Results schema + parquet writer + DuckDB views | `evalkit/results.py`, `evalkit/db.py` | roundtrip test |
| 3 | Matchers (money/date/name/list/box/text) | `evalkit/matchers.py` | unit tests generated by Claude Code, no fixed count (11B) |
| 4 | Stats (bootstrap, paired bootstrap, ECE, calibration bins, Cohen kappa) | `evalkit/stats.py` | known-distribution tests |
| 5 | Config + thresholds loaders with schema | `evalkit/config.py`, `eval/config.yaml`, `eval/thresholds.yaml` | invalid yaml rejected |
| 6 | API clients (Keycloak client-credentials, retry, 429 handling) | `evalkit/clients/{hospital,insurer,gateway,rag,tpasim}.py` | mocked-server tests |
| 7 | Replay layer (record/replay LLM responses) | `evalkit/replay.py`, `eval/replay/` | offline run passes |
| 8 | Runner (concurrency, checkpoint, resume) | `evalkit/runner.py` | kill/resume test |
| 9 | Suites: doc_pipeline, completeness, claim_builder | `evalkit/suites/*.py` | per-suite fixture test |
| 10 | Suites: identity, authenticity, coverage, calculation, query, rag | `evalkit/suites/*.py` | per-suite fixture test |
| 11 | Suite: e2e driver | `evalkit/suites/e2e.py` | S01 completes on live stack |
| 12 | Judge wrapper + judge calibration | `evalkit/judge.py`, `eval/judge_gold/*.json` | kappa ≥ 0.7 measured |
| 13 | Calibration module (reliability, ECE, gate effectiveness) | `evalkit/calibration.py` | plots + json |
| 14 | Reporting (md/html), run diff | `evalkit/report/`, `templates/` | golden-file test |
| 15 | CI jobs and sabotage test | `.github/workflows/eval-fast.yml`, `tests/test_sabotage.py` | fails when sabotaged |
| 16 | Suspected-truth-defect triage tool | `evalkit/triage.py` | lists cross-component disagreements |

## 11B. Annex A — Metric formulas

Notation: for a set of units U with predictions p_u and truths t_u, `correct_u ∈ {0,1}`.

- **Accuracy** = Σ correct_u / |U|. Errors (exceptions) count as `correct=0`.
- **Precision/Recall/F1 (set-valued)**: P = |pred ∩ truth|/|pred| (1.0 if both empty), R = |pred ∩ truth|/|truth| (1.0 if both empty), F1 = 2PR/(P+R) (1.0 if P=R=1).
- **Macro-F1** over classes present in truth: mean of per-class F1.
- **CER** = Levenshtein(pred, ref) / max(1, len(ref)) after Unicode NFKC normalisation and whitespace collapse; reported as mean over units and as pooled (Σedits/Σlen).
- **Field accuracy (strict / lenient)**: strict = normalised exact equality; lenient = name token-set ratio ≥ 0.95, money equal within ±0.01, date equal after parsing.
- **Line match** (bill tables): pair predicted and truth lines by Hungarian assignment on cost = 1 − fuzzy(description) with hard constraint fuzzy ≥ 0.85; matched pair correct if amount equal (strict) and qty equal. Line recall = matched_correct/|truth lines|; precision = matched_correct/|pred lines|.
- **IoU** for boxes = area(A∩B)/area(A∪B); detection counts as TP if IoU ≥ 0.5 and class equal; per-page P/R from greedy matching sorted by score.
- **Masking recall** (per entity class c) = |spans of class c detected with overlap ≥ 0.8 of the char range| / |true spans of c|; precision on non-PII = 1 − (masked chars outside any true span)/(total non-PII chars) at token level.
- **Auto-pass precision** (gate): among units with signal ≥ threshold, fraction correct. **Coverage**: fraction of units with signal ≥ threshold. **Gate lift** = (auto-pass precision − overall accuracy).
- **ECE** with 10 equal-mass bins: Σ_b (n_b/N)·|acc_b − conf_b|. Reported for each gated signal; for non-probability signals (agreement score) they are rank-calibrated into [0,1] by isotonic regression fitted on dev split, evaluated on test split.
- **Flip rate** = fraction of cases whose `correct` differs across repeated runs of an LLM stage (≥ 3 runs).
- **Cost per claim** = Σ (tokens_in×price_in + tokens_out×price_out) for paid aliases; free-tier calls counted as `calls_free`; report both.
- **Human-touch count** = number of human actions (edit, approve, upload, query edit) recorded in audit events per case, from `human.*` event types.
- **Time-to-approval-ready** = timestamp(`claim.built` and status `ready_for_review`) − timestamp(first `doc.uploaded` after `case.created`), wall clock and also "active compute" = Σ stage durations from Langfuse spans.

### Statistical rules
- n < 10 per (metric, archetype): not reported. 10 ≤ n < 30: "low-n" marker. CIs: percentile bootstrap 1000 resamples over cases (not over units) to respect clustering.
- Pass/fail uses the **lower CI bound** for "≥ target" metrics and the **upper CI bound** for "≤ target" metrics when n ≥ 30; for n < 30 uses point estimate and marks "provisional".
- Run comparison: paired bootstrap on per-case differences; regression flagged when 95% CI of delta is entirely < −1 percentage point (deterministic metrics: any decrease).

## 11C. Annex B — Results and run schemas

### B.1 `results.parquet`
| Column | Type | Notes |
|---|---|---|
| run_id | string | `YYYYMMDDTHHMMSS_<gitsha7>` |
| case_id | string | `SYN-…` |
| archetype | string | S01…S25 or `multi:S05+S07` |
| split | string | dev/test/golden |
| component | string | `doc_pipeline`, `vision`, `completeness`, `router`, `builder`, `identity`, `authenticity`, `coverage`, `calc`, `mapper`, `decision_route`, `query_triage`, `query_draft`, `rag`, `e2e` |
| mode | string | component / cascade / e2e |
| unit_id | string | doc_id, field path, line ref, query id |
| metric_name | string | e.g. `field_exact`, `cer`, `stamp_tp` |
| pred | string (json) | |
| truth | string (json) | |
| correct | bool | |
| score | float null | metric-specific numeric (CER, IoU) |
| signal_name | string null | gate signal (e.g. `parse_confidence`) |
| signal_value | float null | |
| run_repeat | int | 0 for single run |
| latency_ms | int | |
| tokens_in/out | int | |
| cost_usd | float | |
| trace_id | string | Langfuse |
| error | string null | `quota`, `timeout`, `exception:<cls>` |

### B.2 `run.json`
```json
{
  "run_id": "20261006T101500_a1b2c3d",
  "started_at": "2026-10-06T10:15:00Z", "mode": "component", "suites": ["doc_pipeline","completeness"],
  "corpus": {"manifest_sha256": "…", "n_cases": 20, "split": "golden"},
  "stack": {"hospital_api": "1.0.3", "insurer_api": "1.0.2", "calc_engine": "0.4.1", "doc_pipeline": "0.5.0"},
  "config_versions": {"doc_requirements": 3, "thresholds": 2, "policy_rules": 5},
  "models": {"cleanup": "gemini-flash-lite@alias:cleanup", "judge": "alias:judge"},
  "prompt_versions": {"intake_cleanup": 7, "query_draft": 4},
  "replay": {"enabled": true, "hit_rate": 1.0},
  "git": {"sha": "a1b2c3d", "dirty": false},
  "thresholds_sha256": "…"
}
```
### B.3 `metrics.json` (shape)
```json
{"doc_pipeline.field_exact.clean": {"value": 0.962, "ci95": [0.951, 0.972], "n": 412, "target": ">=0.95", "pass": true, "low_n": false},
 "completeness.false_complete_rate": {"value": 0.0, "ci95": [0.0, 0.0], "n": 300, "target": "==0", "pass": true}}
```
Key naming: `<component>.<metric>[.<slice>]` where slice ∈ {clean, degraded, all, S07, …}.

### B.4 `eval/thresholds.yaml`
```yaml
doc_pipeline:
  field_exact: {clean: {op: ">=", value: 0.95}, degraded_acceptable: {op: ">=", value: 0.85}}
  cer: {clean: {op: "<=", value: 0.03}}
  classification_acc: {op: ">=", value: 0.97}
  quality_unreadable_recall: {op: ">=", value: 0.95}
completeness:
  false_complete_rate: {op: "==", value: 0, hard: true}
  verdict_acc: {op: "==", value: 1.0, hard: true}
authenticity:
  fpr_clean: {op: "<=", value: 0.03}
  recall_by_tamper: {amount_edit: 0.8, arith: 1.0, metadata: 1.0, duplicate_bill_no: 1.0, default: 0.85}
decision_route: {acc: {op: "==", value: 1.0, hard: true}, wrongful_auto: {op: "==", value: 0, hard: true}}
gates:
  money_field_autopass_precision: {op: ">=", value: 0.98}
e2e:
  median_ttr_minutes: {op: "<=", value: 10}
```
`hard: true` entries fail CI on any violation regardless of CI width.

## 11D. Annex C — Suite specifications

For each suite: **truth source**, **invocation**, **units**, **component vs cascade input**.

### C.1 `doc_pipeline`
- Truth: `docs/*.labels.json`. Invocation: POST file to doc-pipeline `/v1/parse` (04-shared-services doc 02) with `case_id`, `doc_id`; read typed JSON + per-field confidence.
- Units: each label `field` with `required_for_claim` or `is_pii` → `field_exact`; each table row → line match; each doc → `classification`, `quality_class`; each page → stamp/signature detection (via vision-service `/v1/analyze`).
- Slices: `clean` (no degradation), `degraded_acceptable`, `degraded_poor` (expected to be flagged, not accurate), `handwritten`.
- Gate signals recorded: `parse_confidence` (per field), `two_pass_agreement` (per field; whether second pass equals first).

### C.2 `completeness`
- Truth: `expected/hospital.json.completeness`. Component mode: call hospital-api completeness endpoint with TRUE classification payload; cascade: use real classification.
- Units: one per case → `complete` verdict; `missing` set F1; `conditional_triggers` set F1.
- Hard checks: `false_complete_rate`; negative controls from S06 must not over-fire.

### C.3 `claim_builder`
- Truth: `claim_build_fields`. Invocation: builder endpoint (hospital-api trigger) with true parsed docs (component) or real parsed docs (cascade).
- Units: each field path; validity of `ClaimSubmission` (`claim_contract` validation); totals reconciliation.
- Extra: S10 must produce blocker, never silently "fix" totals.

### C.4 `identity`, `authenticity`
- Truth: `expected/insurer.json.identity|authenticity`. Invocation: insurer-crew agent endpoints with submission + docs.
- Identity units: match boolean, mismatch field set; score ROC computed via sklearn-free implementation (sorted thresholds). Authenticity units: flag boolean per case, `reason_codes` set F1, tamper-type recall; FPR on all cases with `tamper=null`.

### C.5 `coverage`, `calculation`, `mapper`
- Coverage truth: `coverage` object; units: three booleans + exclusions set + clause set (recall/precision).
- Calculation: invoke calc-engine `/v1/calculate` with TRUE mapped lines (component) and with mapper output (cascade). Compare `payable` and each deduction `(line_ref, rule_id, amount)`; agreement vs `reference_calc` (the expected value is from the independent reference calc). Disagreement artefact: `calc_disagreements.csv` with both traces side by side.
- Mapper truth: each bill line's policy category from procedures/catalogue; accuracy per category.

### C.6 `decision_route`
- Pure function check: given `payable`, gate results/flags and thresholds version → route (`auto_approve` only when all gates pass and payable ≤ T_auto; otherwise `one_approver` / `two_approvers`; rejections always human). Test grid including boundaries (exactly T_auto, T_auto+0.01, exactly T_four, T_four+0.01) and negative/zero payable.

### C.7 `query`
- tpa-sim scripted per `expected/queries.json`. Units: `triage_category` per incoming query; `draft_grounding` per draft (rule check + judge); `rounds_to_resolution`; `escalation_round`.
- Rule check for numbers/dates: every number or date token in the draft must appear in the case data bundle or KB passages used (after normalisation); violators listed as `ungrounded_numbers`.

### C.8 `rag`
- Truth: `kb/qa.jsonl` lines `{question, clause_ids[], answer_keypoints[]}` (300 generated + 50 hand-written). Units: `recall@5`, `mrr`, `citation_precision` (cited clause ∈ truth), `faithfulness` (judge) per answer.

### C.9 `e2e`
- Drives: create case → upload docs → wait for `ready_for_review` → officer sign-off (service account acts as officer, recorded as human touch) → submission → insurer verification → tpa-sim query script → decision → settlement callback.
- Units: `final_status`, `status_path` (must follow state machine in `01-02` §4), `ttr`, `human_touches`, `agent_errors`, `cost`.
- Timeouts: stage timeout 20 min; whole case 45 min; failures recorded as `error=timeout`.

## 11E. Annex D — Calibration and gate-effectiveness procedure

1. Collect (signal, correct) pairs for each gated unit type from `results.parquet` rows with `signal_name` set, dev split only.
2. For `parse_confidence` and `vision_confidence`: bin into 10 equal-mass bins; compute reliability points and ECE.
3. For `two_pass_agreement` (categorical agree/disagree or similarity): treat agree as signal=1; compute precision of agree = P(correct | agree) and P(correct | disagree).
4. Gate sweep: for thresholds θ in a grid, compute auto-pass precision and coverage; choose smallest θ with precision ≥ 0.98 for money fields (≥ 0.95 for non-money, PROPOSED) — output `proposed_gate_thresholds.yaml` (not applied automatically; change goes through config PR/versioned rows per `01-03`).
5. Evaluate chosen θ on the test split; report precision and coverage with CI; if test precision CI lower bound < target → flag `GATE_UNSAFE`, fail the nightly job.
6. LLM self-reported confidence (if the agent returns one): compute point-biserial correlation with correctness and AUROC; the report states the number to justify "never used for gating".
7. Output: `calibration/<signal>.png` (reliability diagram + histogram), `calibration/summary.json`.

## 11F. Annex E — CLI reference

```
eval run     --suites doc_pipeline,completeness --mode component --split golden --repeat 3 [--replay] [--resume RUN_ID] [--cases SYN-000001,SYN-000002]
eval compare RUN_A RUN_B               # paired bootstrap deltas
eval report  RUN_ID --format html
eval calibrate RUN_ID --signals parse_confidence,two_pass_agreement
eval judge-calibrate --gold eval/judge_gold --alias judge
eval triage  RUN_ID                    # suspected truth defects
eval gate    RUN_ID --thresholds eval/thresholds.yaml   # exit code 1 on failure (CI)
eval record  --suites query --cases … # record LLM responses for later replay
```
Exit codes: 0 pass, 1 threshold failure, 2 infrastructure error, 3 quota-limited (nightly treats as neutral), 4 refused (non-synthetic tenant).

## 11G. Annex F — Replay design

- Key: `sha256(model_alias | prompt_template_id@version | canonical(messages) | params)`; store `{request_meta, response, tokens, ts}` in `eval/replay/<key>.json`.
- Modes: `record` (call real gateway, save), `replay` (error if key missing → counts `replay_miss`; CI requires 0 misses), `passthrough`.
- Implemented as an HTTP client wrapper for the llm-gateway base URL used by the services under test via an env override `LLM_GATEWAY_URL` pointed at a tiny `replay-proxy` (FastAPI) started by the eval harness; services do not know they are being replayed (principle: every LLM call goes through the gateway).
- Prompt changes alter keys → replays invalid → `make eval-record` target regenerates; PR template requires attaching new recorded fixtures when prompt versions change.
- Replay files stripped of C2/C3 by construction (masked prompts only; checked by the privacy lint in 03).

## 11H. Annex G — CI gates (summary)

| Gate | Trigger | Suite | Blocking? |
|---|---|---|---|
| unit-eval | every PR | evalkit unit tests | yes |
| eval-fast | PR touching `hospital/`, `insurer/`, `services/`, `contract/`, prompts | golden 20, component, replay | yes (hard + deterministic metrics) |
| contract | PR touching `contract/` | schemathesis | yes |
| calc-agreement | PR touching calc-engine or reference calc | S01-S16 all cases, calc only (no LLM) | yes (≥ 99.5% and zero unexplained diffs) |
| privacy-egress | nightly + PR touching doc-pipeline/gateway | S25 + corpus sample 50 with egress capture | yes (any C3 hit) |
| eval-nightly | nightly | full corpus test split, cascade, 3 repeats on golden | no (opens an issue); `GATE_UNSAFE` pages the devs |
| sabotage | weekly + PR touching evalkit | broken-mock service | yes |

`eval gate` prints a compact table (metric, value, CI, target, status) and writes `gate.json` for CI annotations.

## 11I. Annex H — Report skeleton (HTML)

Sections and data sources:
1. **Header**: run id, git sha, stack versions, config versions, corpus manifest hash, replay hit rate, quota-limited %.
2. **Summary table**: from `metrics.json`; colour only status (pass/fail/provisional) and always print numbers.
3. **Archetype heatmap**: DuckDB query `SELECT archetype, component, avg(correct::int) …` rendered as grid with n in each cell.
4. **Cascade vs component**: per component delta (component − cascade) with CI; ranks components by error contribution.
5. **Calibration**: plots and gate table (θ, precision, coverage, CI).
6. **Cost/latency**: p50/p95 by component, tokens, calls by alias, free-tier call counts.
7. **Failures**: top 20 by severity (money impact, hard-gate) with input/output diff and trace links; one JSON per failure.
8. **Run diff**: regressions and improvements with CI.
9. **Human load**: touches per claim by role and by archetype.
10. **Suspected truth defects**: from triage.
11. **Appendix**: raw thresholds, environment, seeds.
Accessibility: charts have text tables beneath; colour-blind-safe palette; status never colour-only.

## 11J. Annex I — Sabotage and self-tests

- Sabotage mock services (in `eval/tests/mocks/`): (a) completeness mock that always returns `complete=true` → must fail `false_complete_rate`; (b) calc mock that rounds down → fails calc agreement; (c) doc-pipeline mock that drops 10% of lines → fails line recall; (d) gateway mock that leaks a 12-digit number in a prompt → egress test fails.
- Self-test of CIs: simulate Bernoulli(0.9) with n=200, 1000 times; the 95% bootstrap CI coverage must be in [0.93, 0.97].
- Matcher fixtures (Claude Code generates them; no fixed count): money (`"1,84,520.00"`, `"₹184520"`, `"184520.5"`, `"1.84.520"` invalid), dates (`"04/03/2026"`, `"4 Mar 2026"`, `"2026-03-04"`, `"31/02/2026"` invalid), names (`"Mr. Rohan  Deshmukh"` vs `"ROHAN DESHMUKH"`; `"R. Deshmukh"` vs `"Rohan Deshmukh"` → lenient false, reported), boxes (IoU 0.49 vs 0.5 edge), CER (empty ref, empty pred, Unicode combining marks).
- Resume test: kill after 5 of 20 cases, resume produces identical rows for completed cases and runs the rest exactly once.

## 11K. Additional edge cases

- Components that are not built yet: suite returns `SKIPPED:not_available`, excluded from gating but displayed; prevents blocking early phases.
- Partial-credit rule: never give partial credit for money fields; strict equality (or ±0.01 only in the OCR-line slice reported separately).
- Clock: all times from Langfuse/audit events, not harness wall clock, to avoid skew from queueing in the harness.
- Multi-tenant safety: harness sets header `X-Synthetic-Tenant: true`; APIs in non-synthetic mode refuse (exit code 4).
- Large runs: results written in chunks of 5,000 rows to parquet part files; DuckDB reads the directory.

## 12. Dependencies and Claude Code kickoff prompt
Depends on: 05-01 (corpus), 04-observability (trace ids), all component APIs (stubs initially). Component suites can be written as each component lands: Dev A owns suites 4.1/4.2, Dev B owns 4.3-4.5, joint 4.6/4.7 and shared `matchers.py`, `report/`.

> Implement docs/implementation/05-integration-and-eval/02-evaluation-harness.md: first matchers, results schema, CLI and report skeleton with tests (section 9), then the suites I own (state A or B). Use replay mode for LLM calls in tests. Do not call real providers in unit tests.
