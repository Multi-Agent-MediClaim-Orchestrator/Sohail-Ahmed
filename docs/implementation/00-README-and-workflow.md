# 00 — README and Two-Developer Workflow

Status: PROPOSED (decisions marked PROPOSED fill gaps: the source architecture file has only §1 and §3).

## 1. Purpose
Implementation documents for the Multi-Agent Cashless & Reimbursement Claim Processing system. Two developers, **Dev A (Hospital side)** and **Dev B (Insurer side)**, each drive Claude Code against these docs. Each doc is self-contained enough to be handed to Claude Code as the single source of truth for one unit of work.

## 2. Document map

| Folder | Owner | Contents |
|---|---|---|
| `01-shared-contract/` | Joint (both approve changes) | API contract, shared models, config versioning, audit chain, repo conventions |
| `02-dev-A-hospital/` | Dev A | hospital-db, hospital-api (6 subparts), hospital-n8n, hospital-crew, hospital-ui |
| `03-dev-B-insurer/` | Dev B | insurer-db, insurer-api (5 subparts), calc-engine, insurer-crew, insurer-n8n, insurer-ui |
| `04-shared-services/` | Split (see §3) | doc-pipeline, vision-service, llm-gateway, rag, infra, tpa-sim |
| `05-integration-and-eval/` | Joint | synthetic data, eval harness, security/privacy, observability, E2E |

## 3. Ownership of all 23 containers (exactly one owner each)

| # | Container | Owner | Doc |
|---|---|---|---|
| 1 | hospital-ui | A | 02/10 |
| 2 | hospital-api | A | 02/02-07 |
| 3 | hospital-n8n (+ worker) | A | 02/08 |
| 4 | hospital-crew | A | 02/09 |
| 5 | hospital-db | A | 02/01 |
| 6 | insurer-ui | B | 03/10 |
| 7 | insurer-api | B | 03/02-06 |
| 8 | insurer-n8n (+ worker) | B | 03/09 |
| 9 | insurer-crew | B | 03/08 |
| 10 | calc-engine | B | 03/07 |
| 11 | insurer-db | B | 03/01 |
| 12 | doc-pipeline | A | 04/02 |
| 13 | vision-service | A | 04/03 |
| 14 | llm-gateway (LiteLLM) | B | 04/04 |
| 15 | ollama | B | 04/04 |
| 16 | rag-service | B | 04/05 |
| 17 | qdrant | B | 04/05 |
| 18 | redis | A | 04/01 |
| 19 | MinIO | A | 04/01 |
| 20 | clamav | A | 04/01 |
| 21 | langfuse (+ its Postgres) | B | 04/04 |
| 22 | tpa-sim | B | 04/06 |
| 23 | keycloak | A | 04/01 |

Rule: the owner writes the compose entry, healthcheck, Dockerfile, README and tests. The other developer consumes the service only through its documented API. (Note: hospital-n8n worker and insurer-n8n worker are counted with their n8n server; with Langfuse's own Postgres the compose file has more than 23 processes, but 23 logical components.)

## 4. Phases, deliverables and sync points

| Phase | Dev A | Dev B | Exit gate / sync |
|---|---|---|---|
| 0 (together, ~2 days) | `01-05` scaffold, compose infra, repo | `01-01…04` contract package | **Contract v1.0 frozen** (tag `contract-v1.0`); both can run `make up-infra` |
| 1 | hospital-db, hospital-api auth + cases/documents, redis/minio/clamav/keycloak, doc-pipeline | insurer-db, insurer-api claim receipt, calc-engine, llm-gateway | Each side mocks the other (tpa-sim for A; hospital-sim fixture for B); contract tests green |
| 2 | completeness, router, vision-service, hospital-crew, hospital-n8n | rag, insurer-crew, insurer-n8n, verification orchestration, decision gate | **First real end-to-end claim** (S01 in 05/05) |
| 3 | hospital-ui, query inbox/SSE, submission/outbox | insurer-ui, query loop, escalation, settlement | Query loop round-trip incl. round-3 escalation |
| 4 | eval, hardening, privacy | eval, hardening, observability | Final demo + eval report |

Dependency notes (what each side waits on):
- A needs from B: nothing hard before Phase 3 (uses tpa-sim, owned by B — **B must deliver a minimal tpa-sim by end of Phase 1** that accepts claims and sends status/query/decision callbacks).
- B needs from A: nothing hard before Phase 2 (hospital-sim fixture in `contract/tests`); first real claim needs hospital-api submission.
- Both need llm-gateway (B) — **A can use a local FakeGateway until B ships it**; the real one is due by mid-Phase 1.

## 5. Git workflow
- Monorepo, trunk `main`, short-lived branches `a/<topic>`, `b/<topic>`, `joint/<topic>`.
- Directory ownership as in 01-05 §3 and CODEOWNERS; Dev A must not edit B's directories and vice versa (open an issue or a PR for the owner instead).
- PR title `[A|B|J][component] summary`; squash merge; Conventional Commit in the squash message.
- CI must pass (lint, types, unit, contract). Contract changes need both approvals.
- **Contract change process**: PR touching `contract/` or `docs/implementation/01-shared-contract/`; additive → minor bump, breaking → major; update `CHANGELOG.md`, OpenAPI, tpa-sim and hospital-sim in the same PR.
- Tags: `contract-v1.0`, `milestone-M1…M4`.
- Daily 10-minute sync: blockers on contract, shared services availability. Weekly integration run of the E2E suite.

## 6. How to use a doc with Claude Code
1. Open the repo root in Claude Code.
2. Paste the doc's **kickoff prompt** (last section of every doc).
3. Ask Claude to enter plan mode, confirm the numbered task list, then implement task by task, running the doc's tests after each task.
4. Tick the doc's acceptance checklist, open a PR with the checklist in the description.

Generic prefix for every kickoff prompt:
> Read `docs/implementation/01-shared-contract/*` first. Do not change anything under `contract/` without telling me. Follow the numbered build tasks in order, write tests alongside code, and stop at the acceptance criteria.

Tips: keep one Claude Code session per leaf doc; start a fresh session when switching docs; if a doc is ambiguous, fix the doc in the same PR (docs are source of truth).

## 7. Leaf-doc template (all docs follow it)
1. Goal  2. Inputs/Outputs  3. Data model  4. API/endpoints  5. Build tasks (numbered, with file paths)  6. Key logic/pseudocode  7. Config/env vars  8. Error handling and edge cases  9. Tests  10. Acceptance criteria  11. Dependencies  12. Claude Code kickoff prompt.
(Section numbering inside docs may be merged where a section would be empty, but all twelve topics must be covered.)

## 8. Reading order
1. `01-05` (repo) → `01-02` (models) → `01-01` (API contract) → `01-03` (config) → `01-04` (audit).
2. Your own folder in numeric order.
3. The other side's folder only as needed to understand the contract.
4. `05-integration-and-eval/05` before Phase 2.

## 9. Definition of done (every doc)
- All numbered tasks complete; tests in the doc's test matrix written and passing.
- Acceptance criteria ticked in the PR description.
- Audit events emitted for every state change (01-04), config read through `ConfigService` (01-03).
- No raw PII in logs; no provider keys outside llm-gateway.
- Docs updated if behaviour deviated; deviations flagged PROPOSED→DECIDED.

## 10. Decision log (PROPOSED items to confirm in Phase 0)
| # | Decision | Default | Owner to confirm |
|---|---|---|---|
| D1 | `T_auto` / `T_four` | ₹50,000 / ₹5,00,000. `T_auto` is a starting value; re-tune on the synthetic evaluation set so the false auto-approve rate stays below ~1% (criteria: hard gates, amount percentile, error cost) | B |
| D2 | Auto-approve when all hard gates pass and payable ≤ `T_auto`; humans decide above (one approver ≤ `T_four`, two above) and whenever any gate/flag fails | yes (user decision, replaces earlier "human always confirms") | both |
| D3 | HMAC (not mTLS) between systems | HMAC + TLS | both |
| D4 | Contract v1.1 adds `X-Journey-Id` and doc refresh endpoint | accept | both |
| D5 | Default LLM aliases (Gemini Flash / Flash-Lite, local Llama 3.2 3B) | as stated | B |
| D6 | uv + ruff + mypy toolchain | accept | both |
| D7 | Round count 3, escalate on round 3 | per architecture | B |
| D8 | Two-person rule for thresholds/policy publishing | yes | B |
| D9 | Both hospital and insurer systems are built, as separate systems with a clear contract boundary | yes (user) | both |
| D10 | Localhost demo/student project, not sold or hosted; no commercial-licence concerns | yes (user) | both |
| D11 | SLA escalation reminders at 50%/80% removed. Kept: `due_by`, overdue marking at 100%, escalation on round 3 | yes (user) | both |
| D12 | Default required documents = prescriptions and bills only (medicine prescriptions with matching medicine bills, procedure bills, hospital-charge bills), chronological, bills carry a hospital stamp; conditional-rule mechanism kept, config-driven | yes (user) | A |
| D13 | LLM strategy: Gemini free tier via LiteLLM with the user's own API key; Presidio masks text before any external call; raw ID pages are processed only by local models (Ollama). Weak GPU: Ollama default Llama 3.1 8B quantised, fall back to Llama 3.2 3B | yes (user) | B |
| D14 | Pipeline: MinerU (multi-page layout/table extraction, Indian-billing language flags) → Presidio (Aadhaar/PAN masking) → local LLM cleanup | yes (user) | A |
| D15 | No fixed test-case counts; Claude Code generates tests itself | yes (user) | both |
| D16 | Identity verification is database matching only (no live ID-verification API); all data synthetic | yes | B |
| D9 | Document retention in dev | 30 days then purge | A |

## 11. Risks and mitigations
| Risk | Mitigation |
|---|---|
| 16 GB RAM vs 23 containers | profiles and `mem_limit` (01-05 §5); cloud-only LLM during dev |
| Free-tier LLM quota changes | gateway aliases + fallback to Ollama; cache; eval runs batched |
| Contract drift | contract tests in CI; single package imported by both |
| One dev blocked on the other's service | simulators (tpa-sim, hospital-sim, FakeGateway) delivered first |
| Doc/code mismatch | docs are fixed in the same PR as the code |
| Scope creep into pre-auth/real integrations | out-of-scope list in architecture §1 |

## 12. Open questions (answer in Phase 0)
- Source file lacks §2 and §4-12; PROPOSED decisions above fill the gap — review before Phase 1.
- Whether the demo uses cloud LLMs only or requires offline operation (affects Ollama sizing).
- Whether either developer has a GPU (affects vision-service/model choices).

## 13. Claude Code kickoff prompt (for the whole project)
> I am Dev A (or Dev B). Read docs/implementation/00-README-and-workflow.md and the 01-shared-contract folder. List my phase-1 docs from the table in section 4, propose the order, and start with the first one by following its kickoff prompt.
