# Dev B progress log (keep updated)

Repo root = this folder (docs 00-05 live here too). venv: `.venv` (use `./.venv/Scripts/python`). Run tests from root:
`./.venv/Scripts/python -m pytest <path> -q -p no:cacheprovider`. Docker Desktop needed for `integration` tests (postgres via `tests/support/pg.py`).
NOTE for tooling: avoid giant bash heredocs (they break); use the Write tool for files; small python edit scripts via `python - <<'PYEOF'` work.
Run different packages' tests in separate pytest invocations (conftest name clashes across dirs).

User decisions (override docs): auto-approve tier for payable <= T_auto when all checks clean; no 50/80% SLA escalation;
keep round-3 escalation + four-eyes above T_four; full original doc list (+ stamp check on bills); local-only (Ollama/Gemini key later); MinerU+Presidio.

## Done (tests green)
- contract/python/claim_contract: 452 tests, mypy --strict clean, OpenAPI valid.
- insurer/calc_engine: goldens (15 examples + edges), 12 property tests + differential reference, API, CLI. Coverage ~93%.
- insurer/api: alembic 0001-0013 (round trip tested), models, seeds (3 products/60 policies/180 members/12 hospitals/8 users/config), ConfigService,
  receipt (HMAC/idempotency/validators/SSRF/doc fetch/outbox), verification engine+service+orchestrator (inline), decision gate (+auto tier, approvals, SSE router),
  reviewer/internal/decision routers. Tests: test_receipt (29), test_verification_engine (40), test_verification_flow (7), test_gate (28), test_decision_flow (10).

## Also done (tests green; last run 2026-10-07)
- insurer/api query loop (05), settlement (06), admin/cron/metrics: 207 tests total in insurer/api.
- services/tpa-sim: contract endpoints, scenario DSL + engine (FakeClock), 14 scenarios (2 builtin + 12 YAML), chaos (drop/duplicate/reorder/fail_status), probes, UI, bank sim: 27 tests, goldens in tests/golden.
- infra/llm-gateway: config.yaml + local overlay, callbacks (core.py pure logic), init_keys.py, scripts, CI key grep: 32 tests. NOT run against real LiteLLM/Gemini/Ollama (no key/models here).
- services/rag-service: chunking, hybrid retrieval, temporal filter, ACL (JWT), ingest/reindex/janitor, grounded answers, Qdrant REST store, synthetic KB + 120-row eval set: 43 tests.
  Offline eval (hash embedder + lexical reranker + extractive stub): recall@5 0.93, MRR 0.80, nDCG 0.83, citation precision 0.61 (target 0.9, stub-limited), insufficient-evidence 0.95, temporal 1.0.
  Lexical reranker does NOT improve MRR (T14 unmet); needs the real cross-encoder + nomic embeddings + live Qdrant to claim the doc targets.
- insurer/crew: 7 agents, schemas, validators, tools, prompts (versioned by hash), concurrency gate, idempotency cache: 175 tests. No CrewAI dependency (single structured call per agent); live LLM evals not run.
- calc-engine: non-medical rule plurals fixed; property test p9 tolerance widened (rounding across allocation).

## Also done in the latest session (tests green)
- insurer/n8n: flow generator (19 flows) + hygiene checker (45 tests). Not run against a live n8n.
- insurer/ui: Next.js 14 workspace/cases/approvals/escalations/settlements/admin/login + BFF proxy + MSW mocks; `next build`, `tsc`, 12 vitest tests pass.
- infra: compose (base/insurer/ai/sim; `docker compose config` valid), Dockerfiles, Makefile, .env.example, CI workflow, CODEOWNERS, .gitleaks.toml, README.md, OPERATIONS.md, observability rules + doctor script.
- data/synthetic: generator (25 archetypes, deterministic PDFs, independent reference calc), 25 frozen golden cases, 12 tests.
- eval/run_eval.py: calc vs reference 296/296, gate routing 202/202 and 0 wrongful auto paths, mapper, identity, RAG offline metrics.
- insurer/api/tests/test_golden_e2e.py: 19 golden archetypes through the real insurer-api (Postgres in Docker) match ground truth (route, status, payable).
  Findings from building it: generator needed procedure_group + room `days` to match the API's calc input (generator bugs, not engine bugs).
- security: JWT attack matrix (alg=none, expired, wrong aud/secret, tampered payload/signature), signature tamper matrix, raw-PII log/audit/outbox scan; /v1/ready + operational gauges.

## Open items / known gaps (honest list)
- Image-bound archetypes S04,S07,S08,S17,S23,S25 are labelled only (no Pillow/ReportLab rendering; belongs with doc-pipeline). S20 covered by test_query_flow.
- Real Gemini/Ollama/LiteLLM/Langfuse/Qdrant/n8n/Keycloak/MinerU/Presidio never exercised here. RAG offline citation precision 0.61 (stub) vs 0.9 target; lexical reranker does not improve MRR.
- mypy is not clean on tpa-sim/rag-service/crew (Optional narrowing, ~20 errors); ruff is clean on all new packages. insurer/api and calc_engine not linted/typed this session.
- calc-engine branch coverage ~93% (target 100%).
- UI lacks PDF viewer/evidence overlays/SSE wiring/keyboard shortcuts/admin editors (workspace, approvals, settlements are functional against mocks).
- SEC suites not implemented: egress capture (needs gateway + mitmproxy), fuzz corpus (doc-pipeline), network matrix. E2E levels L2/L3 need Dev A's hospital stack.
- Redis-backed integration run of insurer-api not done (memory fallbacks exercised).

## Next (if continuing)
Wire real keys/models and run: make up-llm, tests/live evals; add PDF viewer + SSE to UI; coverage to 100% in calc-engine; mypy cleanup; joint L2 run with Dev A.

## Deviations log
See contract/CHANGELOG.md and below.
- calc rules JSON follows 07-calc-engine PolicyRules (not 01-03 §4.2.1 / 01-insurer-db §6.1); seeds use the 07 shape.
- Config tables follow contract 01-03 DDL (domain,name; default fallback) instead of 01-insurer-db scope_key '*'.
- Migrations renumbered: 0009 gate, 0010 query loop, 0011 settlement, 0012 views, 0013 misc (doc fetch error, inbound seq, decision finalised_at).
- Coverage: specific-waiting / group-mapped pre-existing are warnings (engine disallows only that group's lines); initial waiting, exclusions, inactive policy are blockers.
- Completeness failure skips dependants (single consolidated query round for missing docs).
- Test helpers live in insurer/api/tests/ins_helpers.py (unique_member gives isolated Gold-policy members).
- crew aliases: ins-smart/ins-fast map to gateway alias reason-cloud (insurer-crew key allows only reason-cloud/reason-local); fallback via gateway -> degraded=true from x-llm-fallback-used.
- crew contexts accept both 03-08 shapes and the slimmer bundles insurer-api sends; query drafter assembles text from template + LLM sentences per finding key.
- rag: citation ids get .2/.3 suffix on collision; policy_product == insurer product_code; reranker is lexical until a cross-encoder is supplied.
- llm-gateway: token budget + allow-list + PII guard run in one hook (pii_guard.ProxyHandler) for fixed order.
