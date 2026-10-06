# 01-05 — Repo Layout, Conventions, Compose and CI

Status: PROPOSED. Joint ownership.

## 1. Goal
One monorepo with a predictable layout and shared tooling so two developers (and two Claude Code sessions) can work in parallel without colliding, run only the part of the 23-container stack they need on a 16 GB machine, and share a single CI.

## 2. Inputs / Outputs
- Input: component list and ports from architecture §3; ownership table in `00-README`.
- Output: repository skeleton, `pyproject.toml` workspace, compose files with profiles, Makefile, pre-commit, CI workflow, RAM budget, `.env.example`, CODEOWNERS.

## 3. Repository tree
```
/
├── contract/                         # JOINT
│   ├── openapi/{claims-v1.yaml, schemas/, config/}
│   ├── python/claim_contract/        # models.py enums.py transitions.py signing.py idempotency.py
│   │                                 # outbox.py inbox.py errors.py middleware.py audit.py audit_events.py
│   ├── tests/{fixtures/, contract/, test_*.py, vectors.sh}
│   └── CHANGELOG.md
├── hospital/                         # DEV A
│   ├── api/    {app/, alembic/, tests/, Dockerfile, pyproject.toml}
│   ├── crew/   {app/, prompts/, tests/, Dockerfile}
│   ├── ui/     {app/, components/, e2e/, Dockerfile}
│   └── n8n/    {flows/*.json, README.md}
├── insurer/                          # DEV B
│   ├── api/ crew/ calc_engine/ ui/ n8n/     (same shape)
├── services/
│   ├── doc-pipeline/ (A)   vision-service/ (A)
│   ├── llm-gateway/ (B)    rag-service/ (B)    tpa-sim/ (B)
├── infra/
│   ├── redis/ minio/ clamav/ keycloak/ (A)
│   ├── qdrant/ langfuse/ ollama/ (B)
│   ├── postgres-init/{hospital.sql, insurer.sql}
│   └── compose/{docker-compose.base.yml, hospital.yml, insurer.yml, shared.yml, ai.yml, sim.yml, README.md}
├── data/{synthetic/, eval/, kb/}
├── docs/implementation/
├── scripts/                          # seed, smoke, audit verification
├── Makefile  pyproject.toml  uv.lock  package.json  pnpm-workspace.yaml
├── .env.example  .pre-commit-config.yaml  .gitleaks.toml  CODEOWNERS
└── .github/workflows/ci.yml
```

## 4. Tooling decisions (PROPOSED)
| Area | Choice | Reason |
|---|---|---|
| Python | 3.12 | typing + perf |
| Env/deps | `uv` workspace (one lockfile) | fast, reproducible |
| Lint/format | `ruff` (E,F,I,B,UP,S,ASYNC) | single tool |
| Types | `mypy --strict` on `contract/`, `calc_engine/`; `--strict` gradually elsewhere | money safety |
| Tests | `pytest`, `pytest-asyncio`, `hypothesis`, `schemathesis`, `testcontainers` | |
| Web | FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, `httpx` | per architecture |
| UI | Node 20, pnpm, Next.js 14 (App Router), TypeScript strict, Tailwind, Playwright | |
| Containers | one Dockerfile per service, multi-stage, non-root, healthcheck | |
| Secrets scan | `gitleaks` in pre-commit and CI | |
| Commit style | Conventional Commits (`feat(hospital-api): …`) | changelog |

## 5. Docker Compose layout and profiles
Compose files are composed: `docker compose -f infra/compose/docker-compose.base.yml -f infra/compose/hospital.yml … --profile X up`.

| Profile | Services | Approx RAM |
|---|---|---|
| `infra` | hospital-db, insurer-db, redis, minio, clamav, keycloak, qdrant | 3.5 GB |
| `llm` | llm-gateway, ollama (3B), langfuse + langfuse-db | 6.5 GB |
| `hospital` | hospital-api, hospital-n8n(+worker), hospital-crew, hospital-ui | 2.5 GB |
| `insurer` | insurer-api, insurer-n8n(+worker), insurer-crew, insurer-ui, calc-engine | 2.5 GB |
| `ai-heavy` | doc-pipeline (MinerU), vision-service, rag-service | 5 GB |
| `sim` | tpa-sim | 0.2 GB |

### 5.1 RAM budget (`infra/compose/README.md` must keep this table current)
| Service | `mem_limit` | Notes |
|---|---|---|
| postgres ×2 | 512 MB each | |
| redis | 256 MB | |
| minio | 512 MB | |
| clamav | 1.5 GB | signature DB load peak |
| keycloak | 768 MB | `KC_HEALTH_ENABLED` |
| qdrant | 512 MB | |
| ollama | 6 GB | Llama 3.2 3B default, 8B only on 32 GB |
| langfuse (+db) | 1 GB | |
| n8n ×2 (+workers ×2) | 400 MB each | |
| FastAPI services | 300 MB each | |
| MinerU doc-pipeline | 3 GB | CPU pipeline backend |
| vision-service | 1.5 GB | ONNX CPU |
Guidance: on 16 GB run `infra + hospital + sim` (Dev A) or `infra + insurer + llm(cloud-only, no ollama)` (Dev B); the full stack needs 32 GB.

### 5.2 Ports (fixed; from architecture §3)
hospital-ui 3000, hospital-api 8000, hospital-n8n 5678, hospital-crew 8010, hospital-db 5432, insurer-ui 3100, insurer-api 8100, insurer-n8n 5679, insurer-crew 8110, calc-engine 8120, insurer-db 5433, doc-pipeline 8200, vision-service 8300, llm-gateway 4000, ollama 11434, rag-service 8400, qdrant 6333, redis 6379, minio 9000 (console 9001), clamav 3310, langfuse 3200, tpa-sim 8500, keycloak 8080.

### 5.3 Compose conventions
Every service: `healthcheck`, `restart: unless-stopped`, `mem_limit`, named network (`hospital_net`, `insurer_net`, `shared_net`; hospital and insurer DBs attach only to their own net), `depends_on: condition: service_healthy`, env from `.env`. Hospital and insurer networks are separate; the only bridge is `shared_net` containing gateway/rag/pipeline/minio — plus HTTPS between the two APIs through a published host port.

## 6. Environment and secrets
- `.env.example` is committed; `.env` is never.
- Prefixes: `HOSP_*`, `INS_*`, `SHARED_*`, `KC_*`, `LLM_*`.
- No provider API keys are used (Ollama cloud models are reached through the local Ollama server, which holds its own sign-in). If one is ever added, only `llm-gateway` may receive it. CI check:
```
! git grep -nE '(GEMINI|OPENAI|ANTHROPIC)_API_KEY' -- ':!services/llm-gateway' ':!infra/compose' ':!.env.example' ':!docs'
```
- HMAC secrets: `HOSP_TO_INS_HMAC_SECRET`, `INS_TO_HOSP_HMAC_SECRET` plus key ids (`HOSP_KEY_ID=hosp-001`).
- DB passwords generated by `make init-secrets` into `.env`.
- Dev TLS: `mkcert` CA; `make certs` writes to `infra/certs/` (gitignored).

## 7. Naming conventions
- DB: tables snake_case plural; PK `id UUID` (uuid7 via `uuid_utils`); `created_at/updated_at timestamptz` UTC; enums as PG enums; FK `<table>_id`.
- Business ids: `claim_ref` `HC-YYYY-NNNNNN`, `insurer_claim_no` `IC-YYYY-NNNNNN`, hospital `HOSP-NNNN`.
- REST: `/v1/...`, kebab-case path segments, JSON snake_case, plural resources, actions as `:verb` suffix (`:publish`).
- Events: `domain.entity.verb`.
- Python: modules snake_case, classes PascalCase, no wildcard imports; settings via `pydantic-settings` (`app/settings.py`).
- Branches: `a/<topic>`, `b/<topic>`, `joint/<topic>`.

## 8. Testing conventions
- Unit tests beside code; integration under `tests/integration/` using compose services (`pytest -m integration`).
- Markers: `unit`, `integration`, `contract`, `e2e`, `slow`, `llm` (skipped in CI unless `RUN_LLM=1`).
- Coverage gates: 80% overall, 100% branch for `calc_engine`, 90% for `contract`.
- LLM calls in unit tests are always faked through a `FakeGateway` fixture; golden-file prompts/outputs live in `tests/golden/`.
- Contract tests run against both servers (`make contract-test BASE=http://localhost:8100`).

## 9. CI (`.github/workflows/ci.yml`)
```
lint → typecheck → unit (matrix: contract, hospital-api, hospital-crew, insurer-api, insurer-crew, calc-engine, services) → contract → build-images → compose-smoke
```
- Path filters: hospital-only changes skip insurer jobs, unless `contract/**` changed (then all run).
- `compose-smoke`: `make up-infra && make smoke` (health endpoints of every infra service).
- Cache: uv cache, pnpm store, Docker buildx layers.
- Required checks on `main`; CODEOWNERS enforce directory ownership and double approval for `contract/` and `docs/implementation/01-shared-contract/`.

### 9.1 CODEOWNERS
```
/hospital/                      @dev-a
/services/doc-pipeline/         @dev-a
/services/vision-service/       @dev-a
/infra/redis/ /infra/minio/ /infra/clamav/ /infra/keycloak/   @dev-a
/insurer/                       @dev-b
/services/llm-gateway/ /services/rag-service/ /services/tpa-sim/   @dev-b
/infra/qdrant/ /infra/langfuse/ /infra/ollama/                 @dev-b
/contract/                      @dev-a @dev-b
/docs/implementation/01-shared-contract/   @dev-a @dev-b
```

## 10. Makefile targets
| Target | Action |
|---|---|
| `make init` | uv sync, pnpm install, pre-commit install, `.env` from example, secrets, certs |
| `make up-infra` / `up-hospital` / `up-insurer` / `up-llm` / `up-ai` / `up-sim` / `up-all` | compose up with profiles |
| `make down` / `make nuke` | stop / stop and delete volumes (confirm prompt) |
| `make migrate-hospital` / `migrate-insurer` | alembic upgrade head |
| `make seed` | seed config and synthetic data |
| `make test` / `test-unit` / `test-int` | pytest |
| `make contract-test` | schemathesis |
| `make lint` / `make fmt` / `make typecheck` | quality |
| `make smoke` | curl all health endpoints |
| `make logs S=hospital-api` | tail |

## 11. Build tasks
1. Create tree (section 3) with `.gitkeep` and per-folder `README.md` stating owner.
2. Root `pyproject.toml` with uv workspace members (`contract/python`, `hospital/api`, `hospital/crew`, `insurer/api`, `insurer/crew`, `insurer/calc_engine`, `services/*`).
3. `contract/python/claim_contract` empty package with `py.typed` and a trivial test.
4. Compose files (section 5) with healthchecks, limits, networks, profiles; init SQL creating DBs/roles (`app_role`, `migrator_role`).
5. `.env.example`, `make init-secrets`, `make certs`.
6. Makefile (section 10), `scripts/smoke.sh`.
7. Pre-commit: ruff, ruff-format, mypy, gitleaks, end-of-file, check-yaml, `no-float-money` local hook.
8. CI workflow with path filters (section 9) and CODEOWNERS.
9. `infra/compose/README.md` — RAM budget and troubleshooting (Docker Desktop memory, WSL2 limit).
10. Root `README.md`: quickstart pointing to `docs/implementation/00-README-and-workflow.md`.

## 12. Edge cases and gotchas
| Issue | Mitigation |
|---|---|
| ClamAV start takes 1-2 min | healthcheck `start_period: 120s`; dependants wait |
| Keycloak realm import race | `--import-realm` plus healthcheck on realm endpoint |
| Docker Desktop limited to <12 GB | profiles; document `.wslconfig` |
| Port clashes with local Postgres | compose maps 5432/5433 — document override variables |
| Windows line endings in shell scripts | `.gitattributes` `* text=auto eol=lf` for `*.sh` |
| n8n workflow JSON churn | export normalised via `scripts/n8n_export.py` (sorted keys, strip ids/timestamps) |
| uv lock conflicts between devs | rebase and `uv lock` on conflict; lock file merges are never hand-edited |

## 13. Test matrix
| # | Check | Expected |
|---|---|---|
| R1 | `make init` on clean clone | succeeds, `.env` has no placeholder secrets |
| R2 | `make up-infra` | all healthy within 3 min on 16 GB |
| R3 | `make test` on empty suites | green |
| R4 | gitleaks on repo | clean |
| R5 | provider-key grep | no hits outside gateway |
| R6 | change only `hospital/` file | insurer CI jobs skipped |
| R7 | change `contract/` | all jobs run |
| R8 | PR editing `contract/` with one approval | blocked |
| R9 | float-money hook on a sample violating file | hook fails |
| R10 | `docker compose config` for each profile | valid, no duplicate ports |

## 14. Acceptance criteria
`make up-infra` healthy on a 16 GB machine; `make test` runs green; gitleaks passes; each owner can start only their own profile; CI path filters work; CODEOWNERS enforce approvals; RAM table accurate to measured `docker stats` within 20%.

## 15. Dependencies
None (first doc to implement). Everything else depends on this.

## 16. Claude Code kickoff prompt
> Create the monorepo scaffolding described in docs/implementation/01-shared-contract/05-repo-layout-and-conventions.md. Do build tasks 1-10 in order, no application logic. Verify with tests R1-R5 and R10 and report RAM measured by `docker stats` after `make up-infra`.
