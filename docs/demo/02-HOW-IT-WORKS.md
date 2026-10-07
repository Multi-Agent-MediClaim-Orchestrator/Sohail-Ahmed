# Vortex — How it works (the code map, the AI crews, the databases)

Read `01-RUNBOOK.md` first (how to start it). This file explains **what each piece of code does**, **how the AI "crew" runs**,
and **where every piece of data is stored**. The claim's journey step by step is in `03-WORKFLOW.md`, and how to look inside the
databases / n8n / crew is in `04-INSPECT-AND-RUN.md`.

---

## 1. The big picture

```
 HOSPITAL SIDE                                                INSURER SIDE
 ─────────────                                                ────────────
 Hospital UI (3100)                                           (Insurer UI 3100-class, built but not browser-tested)
      │                                                                      ▲
      ▼                                                                      │
 hospital API (8100) ──signed HTTPS (HMAC)──►  insurer API (8600) ◄── tpa-sim bank simulator (8500)
   │   ▲  ◄──signed callbacks────────────────     │    ▲
   │   │                                          │    ├── calc-engine (payout maths, in-process or 8620)
   │   └── hospital n8n (5688) ◄── webhooks       │    ├── insurer n8n (5689) ◄── webhooks  (sequences the checks)
   ├── hospital crew (8010)  AI: claim builder,   │    └── insurer crew (8610)  AI: identity, authenticity,
   │       query replies                          │            coverage, line mapping, query drafts, triage, summary
   ├── document pipeline (8200) read/classify     │                    │
   ├── vision service (8300) quality, stamps      │                    └── RAG service (8400) ── Qdrant (6333)
   │                                              │
   ▼                                              ▼
 hospital Postgres (5452)  Redis (6390)  MinIO files (9010)     insurer Postgres (5453)   Redis   MinIO
                          ▲ shared: Keycloak login (8080), ClamAV virus scanner (3310)
                          ▲ AI model server for everything: Ollama (11434), runs on the host
```

Three ideas run through the whole design:

1. **The API is the boss, n8n and the AI are helpers.** Every state change is an API call guarded by rules. A lost or repeated
   n8n run or a wrong AI answer cannot corrupt a claim.
2. **Numbers are never decided by AI.** The payout is calculated by `calc_engine` (plain Python). The AI only explains, maps
   words to categories, drafts text, or reads documents, and its output is checked by code.
3. **Everything that matters is written down twice:** in the database, and in a tamper-evident audit chain.

---

## 2. Code map (where to look for what)

Top-level folders:

| Folder | What it is |
|---|---|
| `contract/` | the **shared language** of hospital and insurer: data models, enums, state machines, request signing, idempotency, audit chain. Package name `claim_contract` |
| `hospital/api/` | the hospital backend (FastAPI + Postgres) |
| `hospital/ui/` | the hospital website (Next.js) |
| `hospital/crew/` | the hospital's AI helpers |
| `hospital/n8n/` | hospital workflows (generated JSON) |
| `insurer/api/` | the insurer backend |
| `insurer/crew/` | the insurer's AI helpers |
| `insurer/calc_engine/` | payout calculator |
| `insurer/n8n/` | insurer workflows (generated JSON) |
| `insurer/ui/` | insurer website (built; not yet run in a browser) |
| `services/doc-pipeline/` | reads documents: text, classification, masking of personal data, field extraction |
| `services/vision-service/` | page quality, stamp detection |
| `services/rag-service/` | policy search + cited answers |
| `services/tpa-sim/` | the simulated insurer/bank used for tests and the bank |
| `data/synthetic/`, `data/eval/` | fake patients/documents generator, scoring tools |
| `infra/` | Docker compose files, Keycloak, MinIO, Redis config |
| `scripts/` | `e2e_full.py` (the demo), `check_insurer_crew.py`, helpers |
| `docs/` | specs; `docs/DECISIONS.md` is the log of every choice |

### Hospital API (`hospital/api/app/`)

| Piece | Files | Does |
|---|---|---|
| start-up | `asgi.py`, `main.py` | builds the app, middleware, routers, background outbox worker |
| endpoints | `routers/cases.py`, `documents.py`, `completeness.py`, `claims.py`, `queries.py`, `callbacks.py`, `audit.py`, `stream.py` | the HTTP surface (about 100 routes, all role-guarded) |
| case lifecycle | `services/cases.py`, `transitions.py` | create case, move between statuses (checked against the state machine) |
| uploads | `services/documents.py`, `filechecks.py` | type/size/virus checks, store in MinIO, trigger n8n |
| required documents | `completeness/rules.py`, `service.py` | "is the claim file complete?" |
| claim building | `services/claim_builder.py`, `claim_validation/rules.py`, `claim_edit.py` | draft versions, validation rules, officer edits |
| submission | `services/submission.py`, `outbox/` | final checks, **outbox** row, signed send with retries |
| insurer messages | `services/callbacks.py`, `queries.py` | receive status/decision/query/settlement callbacks, query inbox |
| audit | `services/audit.py` | writes the hash-chained audit trail |
| live updates | `sse/hub.py`, `routers/stream.py` | Server-Sent Events so the UI updates by itself |

### Insurer API (`insurer/api/insurer_app/`)

| Piece | Files | Does |
|---|---|---|
| start-up | `main.py` | app, contract middleware, background jobs, outbox dispatcher |
| door for hospitals | `routers/hospital_api.py`, `validators.py`, `services/receipt.py` | accept a signed claim (checks V1–V13), create the case, acknowledge |
| fetch documents | `services/docs_fetch.py` | download documents with short-lived links, verify hashes |
| verification | `services/orchestrator.py`, `verification.py`, `verification/engine.py` | run the six steps; **rules live in `verification/engine.py`** |
| decision | `services/gate.py`, `decision.py`, `approval_rules.py`, `decision_builder.py` | who must approve (auto / one / two), voting, final decision |
| queries | `services/queries.py`, `query_logic.py` | questions to the hospital, answers, triage, rounds 1–3, escalation |
| settlement | `services/settlement.py`, `clients/bank_sim.py` | payout request to the (simulated) bank, retries, reconciliation |
| config | `config/defaults.py`, `config/service.py` | thresholds (50,000 / 500,000), policy rules, required documents, versioned |
| money | `insurer/calc_engine/calc_engine/engine.py`, `steps/s00…s12` | the calculation, step by step |

### Contract (`contract/python/claim_contract/`)

`models.py` (claim, decision, settlement shapes) · `enums.py` · `transitions.py` (allowed status changes for both sides) ·
`signing.py` (HMAC signature) · `insurer_side/middleware.py` (checks signature, timestamp, idempotency, rate limit) ·
`insurer_side/outbox.py` (reliable delivery with retries) · `insurer_side/audit.py` (hash chain).

---

## 3. The "Crew AI": what it is and which file runs it

**There is no CrewAI library in this project.** "Crew" here means *a small web service that holds a team of single-purpose AI
agents*. Each agent is a plain Python function with a prompt, a strict output schema, and code that checks what the model said.
(This choice is logged in `docs/DECISIONS.md`.) Both sides have one.

### 3.1 The insurer crew (`insurer/crew/insurer_crew/`) — the one to show

| File | Role |
|---|---|
| **`main.py`** | the entry file: `uvicorn insurer_crew.main:app`. Builds settings, the model client, the RAG client, then `create_app(...)` |
| **`app.py`** | the web service: one `POST` route per agent, token check, request limits, caching, timing, health, `/v1/agents` list |
| **`agents.py`** | **the agents themselves** (seven functions, see below) |
| **`runtime.py`** | talks to the model: `GatewayLLM` sends a chat request to Ollama (`/v1/chat/completions`), asks for JSON that matches the schema, retries with a "fix your JSON" message if invalid; also concurrency limits |
| `schemas.py` | strict input and output shapes (pydantic) for every agent |
| `validators.py` | the safety net: strip forbidden/accusing words, drop amounts the AI invented, neutralise prompt-injection text, check that quotes really appear in the retrieved text |
| `tools.py` | helpers the agents call: RAG client, ICD lookup, bill arithmetic, duplicate report |
| `prompts/*.md` | the seven prompts, versioned (`identity-v1.md`, …) |

The seven agents and their HTTP routes:

| Agent (function in `agents.py`) | Route | What it does | What the code still decides |
|---|---|---|---|
| `identity` | `/v1/identity/analyze` | compares patient vs member vs ID document, names variations | name score, DOB/policy match come from the API |
| `authenticity` | `/v1/authenticity/analyze` | explains signals: stamp missing, total mismatch, duplicate bill, image tamper | the signals are computed by the API; the agent cannot add a finding without evidence |
| `coverage` | `/v1/coverage/analyze` | finds policy clauses for the diagnosis/procedure using **RAG**, quotes them | quotes must be verbatim from retrieved text; if the model finds none, a deterministic fallback quotes the matching line; otherwise "insufficient evidence" |
| `calc_mapper` | `/v1/calc/map-lines` | maps bill lines ("Inj Ceftriaxone") to calculator groups (`medicine`) | keyword rules first; the model only handles unknown lines |
| `query_drafter` | `/v1/query/draft` | writes the polite question to the hospital | numbers/forbidden phrases stripped; template fills gaps |
| `triage` | `/v1/query/triage` | did the hospital's reply resolve the finding? | code check of attached documents overrides the model |
| `supervisor` | `/v1/supervisor/summarize` | one-paragraph summary + conflicts for the reviewer | cannot change any decision |

**How a call flows** (identity example):
`insurer API / n8n` → `POST :8610/v1/identity/analyze` with header `X-Service-Token` and body `{request_id, case_id, context}` →
`app.py` checks token and limits → `agents.identity()` builds the prompt from `prompts/identity-v1.md` + the context →
`runtime.GatewayLLM.complete()` → **Ollama** `gemma4:latest` returns JSON → `validators` clean it → output (with `trace_id`,
`prompt_version`, `model_alias`, `degraded` flag) goes back → the API stores it in `core.verification_step.agent_output`.

Who calls the crew:
- **n8n path:** flow `13_step_identity`, `14_step_authenticity`, `15_step_coverage` → sub-flow `03_call_crew` (`insurer/n8n/build_flows.py`).
- **Inline path:** `insurer/api/insurer_app/services/orchestrator.py`, function `_agent_body` → `clients/crew.py`.
- **Failure policy:** if the crew is down or returns junk, the step is marked degraded and a human checks; the claim is never auto-approved.

### 3.2 The hospital crew (`hospital/crew/crew/`)

| File | Role |
|---|---|
| **`main.py`** | entry: `uvicorn crew.main:app_factory --factory` (port 8010); job endpoints (`/v1/jobs/...`) |
| `jobs.py` | runs jobs in the background and keeps their status (the API polls) |
| **`agents/builder.py`** | **claim builder/repair**: assembles the claim from parsed documents, fixes validation errors |
| **`agents/responder.py`** | **query reply drafter and triage**: writes a grounded reply to the insurer's question |
| `tools/assemble.py`, `categories.py`, `category_map.yaml` | deterministic assembly: bill lines, categories, totals |
| `guards/pii.py`, `grounding.py`, `supervisor.py` | blocks personal data in prompts; every statement must trace to a claim fact; final sanity checks |
| `llm.py` | `OllamaLLM` (real model) and `RulesLLM` (deterministic, no model) |
| `settings.py` | env: `CREW_LLM=ollama|rules`, `HOSP_LLM_MODEL`, `HOSP_LLM_LOCAL_MODEL` |

Called by the hospital API in `hospital/api/app/services/crew.py`.

### 3.3 Safety rules shared by both
- Only **masked** text may go to a *cloud* model; identity documents never leave the machine. Today everything uses the **local**
  model, so nothing leaves the laptop.
- The model never sees or returns a final amount; `validators` drop invented numbers.
- Every call records the prompt version, model name and a trace id.

---

## 4. RAG (policy search with citations) — `services/rag-service/rag_service/`

| File | Role |
|---|---|
| `main.py` | entry: builds the stores and the model clients (`uvicorn rag_service.main:app`) |
| `app.py` | HTTP: `/v1/search`, `/v1/answer`, `/v1/ingest`, `/v1/collections`, `/health`, `/metrics` |
| `corpus.py` | **builds the synthetic knowledge base** (policy wording for HEALTH-BASIC, HEALTH-PLUS-GOLD, SENIOR-SHIELD, medical guidelines, hospital rules, precedents) and the gold question set |
| `chunking.py`, `ingest.py` | cut documents into sections (keeps table rows and headings), embed, store |
| `retrieval.py` | **search**: vector search + keyword search → fuse (RRF) → rerank → filter by product and **effective date** |
| `answer.py` | prompt the model with the chunks, then **validate**: every sentence must carry a citation that exists |
| `gateway.py` | the model clients: embeddings and chat, both through Ollama |
| `qdrant_store.py` | the Qdrant client |
| `security.py` | who may read which collection (JWT per caller: `insurer-crew`, `hospital-crew`, …) |

How a search works: question → embedded with `nomic-embed-text` (768 numbers) → Qdrant dense + sparse search, **filtered** to
the policy product and the date the patient was admitted (so old wording is never returned) → top chunks with citation ids like
`pw-HPB-v2#p11#s5` (= policy wording, product B, version 2, page 11, section 5).

---

## 5. Where everything is stored

| What | Where | How to see it |
|---|---|---|
| **Hospital data** (cases, patients, documents list, drafts, outbox, queries, audit) | Postgres database `hospital`, container `claims-hospital-db-1`, **port 5452**, Docker volume `claims_hospital_pg` | `docker exec claims-hospital-db-1 psql -U hospital -d hospital` |
| **Hospital n8n** (workflows, executions) | database `n8n` in the **same** Postgres | `psql -U hospital -d n8n` (tables `workflow_entity`, `execution_entity`) |
| **Insurer data** (claims, bill lines, verification runs, decisions, approvals, queries, settlements, config, audit) | Postgres database `insurer`, container `claims-insurer-db-1`, **port 5453**, volume `claims_insurer_pg`. Schemas: `core` (28 tables), `config`, `audit`, `ops` | `docker exec claims-insurer-db-1 psql -U postgres -d insurer` |
| **Insurer n8n** | its own SQLite file in volume `claims_insurer_n8n_data` | the n8n website (5689) |
| **Uploaded documents / PDFs** | **MinIO** (S3-style file store), port 9010, volume `claims_minio_data`; buckets `hospital-docs`, `insurer-docs`, `kb-sources`; versioned and encrypted | console `http://localhost:9011` |
| **Fast temporary state** (idempotency keys, caches, rate limits, live-event streams) | **Redis**, port 6390 (ACL users per service) | `redis-cli --user admin` inside the container |
| **RAG vectors** (the policy knowledge base) | **Qdrant**, port 6333, volume `claims_qdrant_data`; collections `ins_policy_wording__v1`, `ins_medical_guidelines__v1`, `hosp_insurer_rules__v1`, `ins_case_history__v1` (126 chunks for policy wording) | `curl -H "api-key: …" localhost:6333/collections` |
| **RAG bookkeeping** (which collection uses which embedding model, a log of searches) | SQLite file `.e2e-logs/rag.db` | `sqlite3` or Python |
| **Logs of the demo run** | `.e2e-logs/*.log` (one per service), generated cases in `.e2e-logs/case_*/` | plain files |
| **Login** | Keycloak (container, port 8080), realms `hospital` and `insurer`, defined in `infra/keycloak/` | `http://localhost:8080` |
| **Secrets and ports** | `.env` (git-ignored) | never share it |

Two databases on purpose: hospital and insurer are separate companies, so neither can read the other's tables. They exchange only
signed API messages.

### The audit chain (tamper evidence)
Every important action appends a row to `audit_event` (hospital) / `audit.audit_event` (insurer) containing the hash of the previous
row. Change any old row and every later hash breaks. `GET /v1/audit/{case}/verify` (hospital) and
`GET /v1/cases/{id}/audit/verify` (insurer) re-check the chain; the demo ends by verifying both.

---

## 6. The contract between the two sides (why messages are safe)

- **Signed requests:** each request carries `X-Key-Id`, `X-Timestamp`, `X-Signature` = HMAC-SHA256 over method, path, time,
  idempotency key and body hash (`contract/.../signing.py`). A wrong secret is refused (401); old timestamps are refused.
- **Idempotency:** `X-Idempotency-Key` makes retries harmless. The same key + same body returns the stored answer; the same key with
  a different body is a conflict (`insurer_side/idempotency.py`, Redis + database).
- **Outbox:** the sender writes the message to an `outbox` table **in the same transaction** as the state change, and a worker
  delivers it with retries (1 s, 4 s, 16 s, … up to 8 tries). So a crash never loses or half-sends a message.
- **Sequence numbers** on callbacks: the receiver ignores old ones and notices gaps.
- **State machines** (`contract/.../transitions.py`): both sides can only move a case along allowed edges.

---

## 7. The decision logic in plain words (`insurer/api/insurer_app/services/gate.py`)

1. The calculator produces the **payable** and a list of deductions.
2. **Auto-approve** only if *all* hold: every check passed (no blockers or warnings), identity score ≥ 0.90, no review flags, the
   run was not degraded, and the amount is ≤ **T_auto = 50,000 INR** (the config value `thresholds.t_auto_inr`).
3. Above T_auto (or any flag): **one human** reviewer decides and one approver signs.
4. Above **T_four = 500,000 INR**: **two** approvers, at least one senior. They must be different people from the submitter.
5. A failed gate always forces a human. A reviewer who changes the system's recommendation must give a reason.
6. Round 3 of questions unanswered → escalation to a senior.

Money in the demo is simulated: the bank is `tpa-sim`, and `INS_SETTLEMENT_MODE=sim` is enforced in code.

---

## 8. Models and speed (measured on this laptop)

- All AI runs through **Ollama** on the host (`localhost:11434`). Default model: `gemma4:latest` (6.6 GB). Embeddings: `nomic-embed-text`.
- The GPU is a 4 GB T1000, so `gemma4:latest` does not fit entirely and part runs on the CPU. That is why calls take seconds, not milliseconds.
- **Where to change a model** (one setting per service):

  | Service | Setting |
  |---|---|
  | insurer crew | `INS_CREW_MODEL` (read by `scripts/e2e_full.py` / `check_insurer_crew.py`), inside the crew `INS_ALIAS_SMART/FAST/FALLBACK` |
  | RAG answers | `RAG_CHAT_MODEL` (Makefile `RAG_ENV`) |
  | RAG embeddings | `RAG_EMBED_MODEL` (changing it means re-running `make seed-kb`) |
  | hospital crew | `HOSP_LLM_LOCAL_MODEL`, `HOSP_LLM_MODEL`, `CREW_LLM=rules` for no model at all |

- **What actually made it faster:** turning the model's "thinking" off for RAG answers (51 s → 3–9 s, same answer and citation).
  The crews already did this (`reasoning_effort: none`).
- **What did not:** a smaller model was *not* automatically better. `llama3.2` (3B) passed only 9 of 11 known-answer checks and was
  slower overall on this machine, so `gemma4:latest` stays the default. Measure with `make check-crew` before switching.
- Do not point insurer data at the remote `gemma4:31b-cloud` model just to be faster: only masked text may go to a cloud model.

---

## 9. What is verified, and what is not (be upfront in a demo)

Verified: all seven scenarios run end to end; the crew passes 11/11 known-answer checks on the real model; n8n 2.41.6 runs both
sides' flows; RAG answers with correct citations; the test suites (463 + 34 + 12 + 63 and the insurer packages), lint and type
checks pass.

Not verified / known limits: the **insurer website** has not been driven in a browser; surgery claims need a procedure-bill document the
synthetic generator does not make yet; the RAG quality numbers from the live evaluation are still being measured; the vector store holds
**synthetic** policy text only; real scans, a real bank and production-grade login are out of scope (local demo, synthetic data).
