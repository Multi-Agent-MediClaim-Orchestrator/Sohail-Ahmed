# Decisions log
Format: decision, reason, date. Newest first. Spec fixes are proven by a test.

## 2026-10-07 — learned stamp detector (replaces "no trained detector")

- The classical colour rule was measured first on a held-out hard-stamp set (90 pages, seed 99; difficulty easy / medium /
  hard = grey photocopies, faint and black ink, overlap with text, heavy JPEG, ink-coloured distractors): box precision /
  recall **0.74 / 0.56 overall, 0.25 / 0.07 on hard pages**.
- New: colour-independent candidates (mid-sized hollow ink components) with 20 shape/ink features, scored by a
  40-tree random forest trained on 450 synthetic pages (seed 7), exported to `vision/stamp_model.json` and run with numpy
  only. Same held-out set: **0.99 / 0.97 overall, 0.97 / 0.97 on hard pages**; page-level "has a stamp" 1.00 / 0.98.
  The existing document-level evaluation (stamp P/R, legibility) is unchanged at 1.00.
- Limits, stated plainly: trained and tested on pages from the same synthetic generator family, so these numbers
  overstate real-world accuracy; real scans are untested. No YOLO/ONNX model (needs torch and a GPU). The classical rule
  remains as the fallback when no model file ships. Retrain with `make train-stamps`, evaluate with `make eval-stamps`;
  scikit-learn is a dev dependency of `data/eval` only. A regression test guards page recall/precision >= 0.9.

## 2026-10-07 — end-to-end run, deterministic modes, and what the e2e found

- `make e2e-hospital` runs the real stack (hospital-api, doc-pipeline, vision, crew, n8n, Keycloak, MinIO, ClamAV,
  Postgres, Redis) against the insurer simulator in 11 steps from case creation to settlement and audit. Default
  `E2E_LLM=rules` uses deterministic extractors (`DOCPIPE_LLM=rules`, `CREW_LLM=rules`): about 20 s. `E2E_LLM=ollama`
  uses the real local model (minutes). It migrates the dev database, clears cached user ids, restarts n8n when the flows
  changed, refreshes tokens, fails fast on stuck states and prints a diagnostic.
- `RulesLLM` (doc-pipeline and crew): model-free extractors with the same interface. They only copy printed values, so
  the evidence check passes by construction and missing fields stay null. They double as the offline fallback.
- Defects the e2e exposed and the fixes (each with a regression test): hospital-api keeps a document `processing`
  until it has two passes, so the pipeline always sends two (cloud on masked text when allowed, else the local model
  re-reading independently); flow F1 read the wrong confidence key (now linted against the pipeline's result keys);
  prescriptions had no `medicines` field although completeness requires it (a test now checks every `must_have_field`
  is producible); a surgical final bill tied with a procedure bill in classification (title keyword now outweighs body
  keywords); person spans crossed column gaps; the crew did not take ICD codes from the discharge summary and the draft
  validator did not flag a missing diagnosis code (V07 now errors at draft time instead of a 422 at sign-off); stale
  cached user ids from the test database broke the first write of the dev API (test session clears them).
- Ops lessons recorded: the dev database must be migrated (`make migrate`) before the API runs; n8n imports flows only
  at boot; access tokens last about five minutes.

## 2026-10-07 — synthetic data and evaluation harness (hospital side only)

- `data/synthetic` (package `synth`): seeded, byte-reproducible hospital-side corpus: 11 archetypes (S01-S03, S05-S08,
  S10, S23-S25), five document types plus implant sticker, stamps/signatures, degradations with a deterministic
  quality class, labels emitted by the renderer, derived hospital expectations, lint that fails on any Verhoeff-valid
  12-digit number outside the reserved 9999 range. NOT built: the insurer side (reference calculator, tamper,
  policy KB, query scripts), loaders other than the file output, golden freezing is a manifest of sha256 only.
- `data/eval` (package `evalh`): runs the doc-pipeline and vision libraries over the corpus and scores classification,
  bill-line/total exactness, stamp precision/recall, legibility and "damaged pages are flagged". `--llm none` scores the
  deterministic parts; `--llm ollama` adds the model passes (slow, opt-in). T_auto tuning needs the insurer's decision
  logic and is not possible from the hospital side.
- The harness found three real defects, fixed with regression tests: vision contrast read 0 on mostly-white pages
  (page called blank / too bright), the skew estimator was fooled by stamp borders (replaced by a projection-profile
  method), outlined rectangular stamps were rejected; and in doc-pipeline the "Net amount" row replaced the printed
  total and descriptions ending in a digit were dropped.

## 2026-10-07 — doc-pipeline and vision-service: what was built instead of the spec's stack

- **Packages are `docpipe` and `vision`**, not `app` (two workspace members called `app` shadow each other in one venv).
- **doc-pipeline:** text layer (`pdftotext`) then tesseract; MinerU is wired as an optional backend and used when its
  CLI is installed, but it is NOT installed here (several GB of models, weak GPU) so that path is untested. Presidio +
  spaCy `en_core_web_sm` for PERSON, plus own recognizers (Aadhaar with Verhoeff, PAN, phone, IFSC, account, policy,
  member, DOB) and a labelled-name rule because the small spaCy model misses "Patient Name: X". Results are unmasked
  locally before going to hospital-api (same trust zone) because completeness compares patient names; only masked text
  reaches the cloud model, and identity documents never reach it. `/v1/unmask` and the MinIO-stored `pii_map.enc` are
  not built (the API receives real values directly). Jobs are in memory. Returned shape is the `passes` list the API's
  parse callback takes (the doc's `ParsedDocument` is not exposed).
- **vision-service:** classical detector only (no trained YOLO/ONNX model: there is no labelled data or GPU),
  tesseract instead of PaddleOCR, escalation through the LOCAL vision model (not a cloud alias), in-memory budgets.
  `has_required_stamp` from `/v1/quality` means "a hospital stamp or seal was seen"; whether the doc type needs one is
  the API's rule. Absence on an unreadable page is reported as unknown (null), not False.
- New API endpoint `GET /v1/internal/hospitals` (stamp-text registry).
- `make test` runs the services in separate pytest invocations; `-m llm` / `-m n8n` tests are opt-in (`make test-llm`,
  `make n8n-test`).

## 2026-10-07 — UI (doc 10): lean Next.js app, deviations

- **npm, not pnpm** (pnpm is not installed). No shadcn/NextAuth/Monaco/PDF.js/MSW/Playwright: plain Tailwind
  components, a cookie-session proxy, a JSON textarea for config, and "Open" via the presigned download URL.
- **Auth:** password grant against the dev client (`hospital-dev`) with server-side token storage, not NextAuth PKCE.
  Fine for localhost; a real deployment should switch to the authorization-code flow.
- **Audit explorer / hash-chain verify, saved views, ICD search, route-preview banner, PDF viewer with provenance
  jump, version-history diff** are not built (no API endpoint or out of demo scope); the case Timeline tab shows status
  history. Query reply editor shows grounding warnings and sources, not clickable citation chips.
- **New API endpoint:** `GET /v1/dashboard/summary`. hospital-ui listens on 3100 (3000 is taken on this machine).
- **Verification done:** typecheck, lint, vitest, production build, and login → proxy → API → SSE exercised through
  the running UI server with curl. **Not done:** visual/browser check (the Chrome extension was not connected).

## 2026-10-07 — crew service (doc 09): plain agents over Ollama, no CrewAI package, no classify-extract job

- The `crewai` package is not installed: it pulls a very large dependency tree for orchestration the jobs do not
  need (each job is one or two prompts plus deterministic code). Agents are plain classes with the roles, guards and
  prompt files from the doc; the HTTP surface (`/v1/jobs/*`) is unchanged, so CrewAI can be swapped in behind it.
- `classify-extract` and `supervise` as LLM jobs are not built: the doc-pipeline (MinerU → Presidio → Ollama, two
  passes) does classification and extraction, and the supervisor is a deterministic rule set (an LLM tone check is
  not a gate). Jobs: `claim-build`, `claim-repair`, `query-triage`, `query-draft`.
- Models: general `gemma4:31b-cloud` only sees identity-free text (the context endpoints drop the patient block);
  line categorisation uses the local model. The PII guard runs before every call.
- Job store is in memory (jobs are minutes long; the API/n8n retry a lost one). Inbound auth is "localhost only";
  the crew's outbound calls use a client-credentials token.
- New API endpoints: `GET /v1/internal/cases/{id}/build-context`, `GET /v1/internal/queries/{id}/context`.
- Live check: local `gemma4:latest` returns valid triage JSON in JSON mode (`pytest -m llm`, ~60 s cold).

## 2026-10-07 — n8n flows (doc 08): thin orchestrator over what the API already does

The API already runs its own completeness scheduler, document sweeper, claim-build crew call, outbox worker and
overdue job. Duplicating that in n8n would give two owners of the same state, so the flows are adapted:

- **F1** skips the crew `classify-extract` step and the VLM escalation: the doc-pipeline (MinerU → Presidio → Ollama,
  two passes) classifies. n8n posts each pass to `/parse`, then `/classify`.
- **F2/F3/F4/F5c/F6/F7** are thin: notify, watchdog, or a cron wrapper around an existing API job. F3 returns stuck
  builds (the API starts the crew itself); F4 has two checks (15 min, 8 h) instead of a 32-iteration loop; F6 fires
  in-app reminders only (no SMTP in a localhost demo; email items are marked failed with a reason).
- **Idempotency** is `POST /v1/internal/idempotency` (Redis `SET NX` under the permitted `idem:hosp:*` prefix) so n8n
  needs no Redis credentials. Token caching is dropped (client-credentials is cheap).
- **U3 polling** is one bounded Code node (loop limited by `max_wait_s`), not a Wait-node loop.
- **Single n8n process** on the host network (same `localhost` URLs and token issuer as everything else); queue mode
  and a worker add a container for no gain at this scale. Postgres database `n8n`, role `n8n_app`.
- Cron flows also expose a secured `jobs/*` webhook so ops (and tests) can run them on demand; `n8n execute`
  cannot start a schedule trigger.
- Tests run against a real n8n container with a stub for every downstream service (`hospital/n8n/tests`).
- New internal endpoints: `idempotency`, `documents/{id}`, `cases/{id}/notify`, `cases/{id}/ack-status`,
  `ops/events`, `reminders/due|fired|failed`, `queries/{id}`, `cases/stale-building`, `outbox/stalled`.
- hospital-api listens on **8100** locally (8000 is taken by another project on this machine).

## 2026-10-07 — LLM stack: Ollama only, no Gemini key (user decision)
- General model: Ollama `gemma4:31b-cloud` on the existing server at `localhost:11434` (verified: answers in ~1.5 s, capabilities completion/thinking/tools/vision). Private/raw-ID pages: local `gemma4:latest` (7.5B, vision, runs locally) and deterministic code. Hugging Face models only where a task needs a specialised model (stamp detection, OCR), chosen when the vision-service is built.
- Reason: the user has no Gemini key and already runs Ollama. Cloud inference leaves the machine, so only Presidio-masked text goes to `gemma4:31b-cloud`; this keeps the earlier privacy rule (raw IDs never leave).
- Dev B follow-up: `llm-gateway`/LiteLLM docs still mention Gemini; point the aliases at Ollama (`ollama_chat/gemma4:31b-cloud`). Dev A talks to Ollama's OpenAI-compatible `/v1` through `HOSP_LLM_BASE_URL` so either the gateway or Ollama directly works.
- The shared Ollama server belongs to the machine, not this repo: do not stop, reconfigure or pull models into it without asking.

## 2026-10-07 — Upload pipeline: virus scan runs before the file-type check (spec fix)
- Doc 03's pipeline sniffed the type first, but its own test expects an EICAR upload to be rejected as `infected`; plain EICAR text fails the sniff, so it would have been reported as `unsupported_media_type` and never quarantined. Scanning first is also safer (infected files with fake extensions are quarantined). Cost: unsupported types are scanned too. Test: `test_eicar_quarantined_and_unreachable`.
- Duplicate guard `(case_id, sha256)` is now a partial unique index excluding deleted documents (migration 0016) so a deleted document can be re-uploaded.
- `alembic_version` needed an explicit read grant for the app role (migration 0015) or `/v1/ready` fails.
- asyncpg cannot infer the type of a bare NULL parameter in `:x IS NULL`; filters use `CAST(:x AS type)`.

## 2026-10-07 — Contract middleware strictness found by schemathesis (spec fixes)
- `X-Idempotency-Key` is required (UUID) on mutating calls; missing/non-UUID -> 400 `bad_request`. Reason: 01-01 §3.1 says required but nothing enforced it.
- `X-Request-Id` (`^[A-Za-z0-9._-]{1,64}$`) and `X-Journey-Id` (UUID) are validated when present; present-but-empty is malformed.
- Required signature headers must be present even in presence-only test mode; wrong method/unknown route passes through to the router (405/404).
- OpenAPI: common 400/401/413/429 documented on every protected operation; `X-Contract-Version` pattern limited to supported major `^1\.\d+$`; spec is 3.1 (pydantic emits JSON-Schema 2020).
- `positive_data_acceptance` is excluded from the schemathesis run: models carry business validators (totals reconcile) that a schema generator cannot satisfy.

## 2026-10-06 — Hospital DB spec fixes (each with a test)
- `next_claim_ref()` raced on `CREATE SEQUENCE IF NOT EXISTS` (pg_class unique violation under concurrency). Fix: advisory lock around lazy creation, `SECURITY DEFINER`. Test: 300 concurrent calls unique and gapless. Migration 0014 adds an optional year argument so rollover is testable.
- `hosp_readonly` column-level REVOKE had no effect on a table-level SELECT grant. Fix: revoke table SELECT, grant an explicit column list excluding `phone_enc`/`id_proof_hash`. Test: SELECT of those columns fails.
- Hospital code is `HOSP-0001` (contract pattern `^HOSP-\d{4}$`), not `DEMO-HOSP`.
- Seed users are the 4 realm users (desk1, officer1-2, hadmin) with fixed ids from `hospital/api/seed/users.json`, shared with the Keycloak realm renderer.
- `alembic check` ignores indexes and CHECK constraints (they live in SQL migrations).
- Doc 04/05/06 define their own migration numbers (0012/0014); real numbering follows the repo: 0014 is `claim_ref_year_param`, later migrations continue from 0015.

## 2026-10-06 — Infra
- MinIO `mc` image/binary are no longer published: bootstrap uses the Python SDK (`infra/minio/init.py`), run from the host.
- ClamAV listens on all interfaces (healthcheck resolves `localhost` to IPv6).
- Host ports shifted in local `.env` (5452, 6390, 9010/9011) because other projects use the defaults.

## 2026-10-06 — Product decisions (user)
- Auto-approve when all gates pass and payable <= `T_auto` (start 50,000 INR); one human above; two above `T_four` (5,00,000). Failed gate/flag forces human review. Round-3 escalation stays; no 50%/80% SLA reminders.
- Default required documents: prescription, pharmacy_bill, final_bill (+ procedure_bill when surgery, implant_sticker for implants); bills hospital-stamped; chronological check.
- Localhost demo, not for sale. All data synthetic. Generate tests rather than fixed counts.

## MinerU evaluated (local, CPU) — tesseract stays the default
- MinerU 4 installed in `.venv-mineru` (gitignored); models via `mineru-kit models download --tier standard`. The adapter now
  calls `mineru-kit parse FILE -o DIR --tier basic` (offline; `MINERU_BIN` overrides; never `--remote`), turns HTML tables
  into spaced text, and is used only when `DOCPIPE_PARSER=mineru` (no longer picked by `auto`).
- Synthetic corpus, 10 cases / 39 documents, rules-only extraction: classification 0.974 for both; line/total exactness
  0.12 (MinerU) vs 0.00 (tesseract, rules-only cannot read OCR table rows); seconds per document 3.1 vs 0.46.
  MinerU is about 7x slower on CPU with no classification gain, so it is opt-in. Real scans not tested (all data synthetic).
