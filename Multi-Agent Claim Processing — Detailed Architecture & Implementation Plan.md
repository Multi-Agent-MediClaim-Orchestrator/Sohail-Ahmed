# Multi-Agent Cashless & Reimbursement Claim Processing — Detailed Architecture & Implementation Plan

Oct 6, 2026 · @soahil

## 1. Purpose, scope and principles

We build two independent localhost systems — a Hospital system and an Insurer/TPA system — that process cashless and reimbursement claims through one configurable pipeline and talk only through a versioned HTTPS API. The goal is to cut discharge-to-approval-ready time from hours to minutes while keeping every decision above a threshold with a human.

### In scope

- Cashless and reimbursement claims for planned and emergency admissions, driven by one claim-type flag.
- Document intake, parsing, classification, completeness, claim building and submission (hospital).
- Identity, authenticity, policy validity, calculation and decision gate (insurer).
- Multi-round insurer query loop with triage, grounded drafts and escalation on round 3.
- Admin consoles for policies, document requirements, thresholds, users and audit.
- An evaluation harness on synthetic data with ground truth.

### Out of scope

- Pre-authorisation (a simulated pre-auth record is assumed to exist).
- Real insurer, TPA, bank or ABDM/NHCX integrations; payments are simulated.
- Real patient data, hosting, and any commercial use.

### Assumptions

- One developer machine: 16 GB RAM minimum (32 GB preferred), weak or no GPU, Docker Desktop.
- Free-tier API key (Gemini Flash / Flash-Lite) plus local Ollama; quotas can change.
- All members, hospitals, policies and documents are synthetic (Faker + ReportLab).

### Design principles

1. **Deterministic first.** Routing, completeness, identity matching, arithmetic and payout calculation are code. LLMs handle only unstructured text and mapping.
2. **Configuration over code.** Document lists, deadlines, thresholds and policy rules are versioned database rows; every decision records the versions it used.
3. **Human authority.** The system recommends; humans decide above T\_auto, and two humans above T\_four.
4. **Privacy by construction.** Raw IDs never leave the machine; Presidio masks text before any external LLM call.
5. **Evidence over confidence.** Gates use parser confidence, two-pass agreement and code checks — never an LLM's self-reported confidence.
6. **Everything auditable.** Every agent output, human edit and approval is an append-only, hash-chained event.
7. **Fixable before fatal.** A missing document or stamp becomes a needs-info request, not a rejection.

## 3. Component architecture

The solution is 23 containers in three groups: hospital-owned, insurer-owned and shared infrastructure. Each system owns its API, workflows, crew, database and UI; shared services are stateless or namespaced per system so neither side can read the other's data.

### Hospital system

| Component | Tech | Port | Responsibilities | Talks to |
| --- | --- | --- | --- | --- |
| hospital-ui | Next.js 14 (App Router) | 3000 | Desk, Officer and Admin screens; SSE status feed | hospital-api |
| hospital-api | FastAPI + SQLAlchemy 2 + Alembic | 8000 | Cases, documents, completeness, router, claim builder trigger, submission, query inbox, auth | hospital-db, object store, n8n, crew, insurer-api |
| hospital-n8n | n8n CE (queue mode, worker) | 5678 | Intake flow, completeness loop, submission, query loop, reminders | hospital-api, crew |
| hospital-crew | CrewAI service behind FastAPI | 8010 | Document Intake Agent, Claim Builder, Query Responder, Supervisor | LiteLLM, pipeline, RAG |
| hospital-db | PostgreSQL 16 | 5432 | Hospital schema (section 8) | — |

### Insurer/TPA system

| Component | Tech | Port | Responsibilities | Talks to |
| --- | --- | --- | --- | --- |
| insurer-ui | Next.js 14 | 3100 | Reviewer, Approver and Admin screens; claim review workspace | insurer-api |
| insurer-api | FastAPI | 8100 | Claim receipt, case management, verification orchestration, decisions, queries, settlement | insurer-db, object store, n8n, crew, calc engine |
| insurer-n8n | n8n CE | 5679 | Verification flow, decision gate, human wait nodes, query raising, escalation | insurer-api, crew |
| insurer-crew | CrewAI service | 8110 | Identity, Authenticity, Coverage, Calculation mapper, Supervisor | LiteLLM, RAG, calc engine |
| calc-engine | Pure Python package + FastAPI | 8120 | Applies caps, sub-limits, co-pay, deductibles; fully unit-tested | insurer-db (policy params) |
| insurer-db | PostgreSQL 16 | 5433 | Insurer schema (section 8) | — |

### Shared services

| Component | Tech | Port | Responsibilities |
| --- | --- | --- | --- |
| doc-pipeline | FastAPI + MinerU (pipeline backend) + Presidio | 8200 | Parse → entities → mask → LLM cleanup → typed JSON |
| vision-service | FastAPI + ONNX Runtime (CPU) + OpenCV + PaddleOCR | 8300 | Image quality, stamp detection, stamp OCR, vision escalation |
| llm-gateway | LiteLLM proxy | 4000 | Model aliases, routing, fallback, caching, rate limits, budget, Langfuse callback |
| ollama | Ollama | 11434 | Llama 3.1 8B / 3.2 3B for cleanup and local fallback |
| rag-service | LlamaIndex + FastAPI | 8400 | Ingestion, hybrid retrieval, reranking, citations |
| qdrant | Qdrant | 6333 | Vector store, one collection per system and purpose |
| redis | Redis 7 | 6379 | n8n queue, LiteLLM cache, SSE pub/sub, idempotency keys |
| object store | MinIO | 9000 | Buckets `hospital-docs`, `insurer-docs`, `kb-sources`; SSE-S3 encryption |
| clamav | ClamAV daemon | 3310 | Virus scan on every upload |
| langfuse | Langfuse (self-hosted) + its Postgres | 3200 | Traces, prompt versions, cost and latency per claim |
| tpa-sim | FastAPI + small UI | 8500 | Simulated insurer messages, scripted query generator, settlement callbacks |
| keycloak | Keycloak | 8080 | Two realms (`hospital`, `insurer`), roles, OIDC for UIs and APIs |

### Interface rules

- UIs call only their own API; APIs never share a database.
- Hospital ↔ insurer traffic goes only through the versioned REST contract (section 9), signed with HMAC and an idempotency key.
- n8n calls APIs with a service account; crews never write to databases directly — they return Pydantic objects that the API validates and persists.
- Every LLM call goes through LiteLLM; no service holds a provider key except the gateway.
