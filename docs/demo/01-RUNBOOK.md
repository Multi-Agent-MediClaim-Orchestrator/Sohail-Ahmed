# Vortex — Demo runbook (what to type, what it means, which code it runs)

Vortex = the multi-agent cashless and reimbursement **claim processing** system: a **hospital side** (uploads documents, builds the
claim, submits it) and an **insurer side** (verifies, calculates, decides, settles), joined by a signed API contract.
Everything runs on your laptop with synthetic data. Nothing here pushes to GitHub or calls a paid service.

This file is only about **running it**. How the code works is in `02-HOW-IT-WORKS.md`; the claim's journey step by step is in
`03-WORKFLOW.md`.

Project folder (every command below is run from here):

```bash
cd Sohail-Ahmed        # the folder you cloned the repository into
```

---

## One command (start here)

| Command | What it does | Time |
|---|---|---|
| `make demo-check` | Checks Docker, Ollama and its models, RAM and free ports; changes nothing. | seconds |
| `make demo` | Everything on the real local model: `.env` secrets, packages (incl. CrewAI), missing Ollama models, all containers, databases, the knowledge base (Qdrant + `nomic-embed-text`), rag-service, then one claim end to end through both CrewAI crews (hospital `ClaimFlow` with the admissible-amount estimate, insurer CrewAI agents), decision and settlement, then the evaluation report. | about 10 min per claim on `gemma4:latest` |
| `make demo-offline` | The same pipeline with no model: rules for the hospital agents, a deterministic stand-in for the insurer agents, an in-memory knowledge base. The backup for the viva. | about 2-3 min |
| `make demo-down` | Stops the demo's background services and all containers (data volumes are kept; `make nuke` deletes them). | seconds |

Options: `DEMO_SCENARIO=all make demo-offline` runs every claim scenario (default `auto`); `DEMO_ORCH=n8n make demo` lets the
insurer's n8n flows sequence verification (default `inline`). Every step is idempotent, so a failed run can simply be started
again; each step logs to `.e2e-logs/demo/<step>.log`. The sections below are the same steps by hand.

## 0. Before you start (check, don't install)

| Check | Command | What you should see | Why |
|---|---|---|---|
| Docker is running | `docker ps` | a table (other projects' containers are normal) | Postgres, Redis, MinIO, Keycloak, n8n, Qdrant run as containers |
| Ollama is running | `ollama list` | `gemma4:latest` and `nomic-embed-text:latest` in the list | the local AI model (answers) and the embedding model (RAG search) |
| `uv` and `node` exist | `uv --version && node --version` | version numbers | `uv` runs the Python code, `node` the UIs |

`make` is the project's remote control. Each `make <name>` is a short recipe in the file **`Makefile`** (open it and search the name).

---

## 1. One-time setup (skip if already done)

| # | Command | What it means | Code it runs |
|---|---|---|---|
| 1 | `make init` | Create the `.env` settings file, generate random passwords, install all Python packages. | `Makefile` → `init` → `scripts/gen_secrets.py` (fills every `__GENERATE__` in `.env`), then `uv sync --all-packages` |

`.env` is private (git-ignored). It holds ports, passwords and secrets. Never paste it anywhere.

---

## 2. Start the background services (infrastructure) — in this order

Each is a Docker container, started once and left running.

| # | Command | What it means | Code it runs / where | Ports |
|---|---|---|---|---|
| 2 | `make up-infra` | Start the shared infrastructure: hospital database, Redis, file storage, virus scanner, login server. Then create the file-storage buckets. | `Makefile` → `render` (`scripts/render_infra.py`, `infra/keycloak/render_realms.py` write config files) → `docker compose` using `infra/compose/shared.yml` → `infra/minio/init.py` (buckets) | Postgres **5452**, Redis **6390**, MinIO **9010** (console **9011**), ClamAV 3310, Keycloak **8080** |
| 3 | `make up-insurer` | Start the **insurer's own** database. | `infra/compose/shared.yml` service `insurer-db` | Postgres **5453** |
| 4 | `make up-n8n` | Start **n8n** for the hospital side (the workflow engine) and load the hospital's 13 flows. | `infra/compose/hospital.yml` service `hospital-n8n`; at boot `hospital/n8n/entrypoint.sh` imports `hospital/n8n/flows/*.json` | **5688** |
| 5 | `make up-insurer-n8n` | Start n8n for the **insurer** side and load its 19 flows. | `infra/compose/insurer.yml` service `insurer-n8n`; `insurer/n8n/entrypoint.sh` creates the login credential, imports `insurer/n8n/flows/*.json`, publishes the ones listed in `insurer/n8n/active_flows.txt` | **5689** |
| 6 | `make up-rag` | Start **Qdrant**, the vector database that stores the policy knowledge base for RAG. | `infra/compose/ai.yml` service `qdrant` (profile `rag`) | **6333** |
| 7 | `make seed-kb` | Fill Qdrant with the synthetic policy wording (turns text into vectors using the `nomic-embed-text` model). | `Makefile` → `services/rag-service/scripts/seed_kb.py` → `rag_service/corpus.py` (builds the documents) → `rag_service/ingest.py` (cuts into chunks, embeds, stores) | — |

Check everything is up:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep claims
```

You should see `claims-hospital-db-1`, `claims-insurer-db-1`, `claims-redis-1`, `claims-minio-1`, `claims-keycloak-1`, `claims-clamav-1`,
`claims-hospital-n8n-1`, `claims-insurer-n8n-1`, `claims-qdrant-1`.

---

## 3. Start the RAG service (leave this terminal open)

Open a **second terminal**, `cd` to the project folder, then:

```bash
make run-rag
```

| What it means | Code it runs |
|---|---|
| Start the RAG web service. It answers "find the policy clause about X" by searching Qdrant, and can write a cited answer with the local model. | `Makefile` → `RAG_ENV` (sets Qdrant URL, Ollama as the model server, `CHAT_REASONING_EFFORT=none` for speed) → `uvicorn rag_service.main:app` → `services/rag-service/rag_service/main.py` (`build()`), endpoints in `rag_service/app.py` |
| Port **8400**. Check: `curl localhost:8400/health` → `{"ok":true,...}` | |

If it says **"address already in use"**, a RAG service is already running — that is fine, do not start another. To restart it:
`ss -ltnp | grep :8400` shows the pid, then `kill <pid>` and `make run-rag`.

---

## 4. Prepare the databases (the demo script also does this, but know what it is)

| Command | What it means | Code it runs |
|---|---|---|
| `make migrate` | Create/upgrade the **hospital** database tables. | `hospital/api/alembic/versions/` (migrations 0001–0022) via `alembic upgrade head` |
| `make seed` | Insert demo hospital `HOSP-0001`, config (required documents, router rules, deadlines). | `hospital/api/seed/run.py`, `seed/config_payloads.py` |
| `make migrate-insurer` | Create/upgrade the **insurer** database tables (schemas `core`, `config`, `audit`, `ops`). | `insurer/api/alembic/versions/` (0001–0013) |
| `make seed-insurer` | Insert 3 products, 60 policies, 180 members, 12 hospitals, 8 users and the insurer's rules/thresholds. | `insurer/api/insurer_app/seeds/seed.py` (profile `demo`) |

---

## 5. THE DEMO — one command runs a whole claim

```bash
E2E_ORCH=n8n E2E_CREW=ollama E2E_SCENARIO=reviewer make e2e-full
```

Read it left to right:

| Piece | Meaning |
|---|---|
| `E2E_ORCH=n8n` | the insurer's verification is sequenced by **n8n** (without it the insurer API does the same steps in-process) |
| `E2E_CREW=ollama` | start the **insurer crew** and let the agents use the real local model (`gemma4:latest`). Leave it out for the fast, model-free version |
| `E2E_SCENARIO=reviewer` | which story to play (table below). `auto,dual,reject` runs several; `all` runs all seven |
| `make e2e-full` | `Makefile` → `scripts/run_exclusive.sh` (a lock so two runs never overlap) → **`scripts/e2e_full.py`** |

What `scripts/e2e_full.py` does, in order (this is what you narrate):

1. Checks n8n and Keycloak are reachable; runs the migrations and seeds from section 4.
2. **Starts the application services itself** (each logs to `.e2e-logs/<name>.log`):

   | Service | Port | Code |
   |---|---|---|
   | insurer API | 8600 | `insurer/api/insurer_app/main.py` |
   | insurer crew (if `E2E_CREW=ollama`) | 8610 | `insurer/crew/insurer_crew/main.py` |
   | tpa-sim (the simulated bank) | 8500 | `services/tpa-sim/tpa_sim/main.py` |
   | hospital API | 8100 | `hospital/api/app/asgi.py` |
   | document pipeline | 8200 | `services/doc-pipeline/docpipe/main.py` |
   | vision service | 8300 | `services/vision-service/vision/main.py` |
   | hospital crew | 8010 | `hospital/crew/crew/main.py` |

3. Generates a synthetic patient case + documents for a **real member from the insurer's data** (`data/synthetic/synth/case.py`).
4. Plays the scenario through both systems, printing one line per step: `- <step> ... ok (1.2s)`.
5. Ends with `FULL E2E PASSED` (exit code 0) or prints logs of what failed.

### The seven stories (`E2E_SCENARIO=`)

| Name | Story | Shows |
|---|---|---|
| `auto` | small clean claim | approved **with no human**, because every check passed and the payable is under the 50,000 INR auto limit |
| `reviewer` | claim above 50,000 | one human reviewer decides, one approver signs |
| `dual` | claim above 500,000 | **two** approvers, one of them senior |
| `reject` | reviewer rejects | hospital sees "rejected", nothing is paid |
| `queries` | insurer asks questions | two answered rounds; round 3 goes unanswered → **escalation** to a senior who decides |
| `callbacks` | replayed / out-of-order messages | duplicates and stale updates do not change a settled claim; a bad signature is refused |
| `bank_retry` | bank fails the first payout | the insurer retries; the hospital still ends **settled** |

Typical times: `auto` ≈ 20 s without the crew; with the real model add a few seconds per agent step.

---

## 6. Show the data live (great during the demo)

Hospital view of the claim (hospital database, container `claims-hospital-db-1`):

```bash
docker exec claims-hospital-db-1 psql -U hospital -d hospital -c "select claim_ref, status from claim_case order by created_at desc limit 5"
```

Insurer view of the same claim (insurer database):

```bash
docker exec claims-insurer-db-1 psql -U postgres -d insurer -c "select insurer_claim_no, status, claimed_amount, approved_amount from core.claim_case order by created_at desc limit 5"
```

The tamper-evident history (every action is chained with a hash; the insurer's copy):

```bash
docker exec claims-insurer-db-1 psql -U postgres -d insurer -c "select seq, event_type, actor_type from audit.audit_event order by ts desc limit 12"
```

What each AI agent said about the last claim (the stored crew output):

```bash
docker exec claims-insurer-db-1 psql -U postgres -d insurer -c "select step, status, score from core.verification_step order by started_at desc limit 6"
```

RAG, searching the policy by hand (the service token comes from `make rag-tokens`):

```bash
TOK=$(make -s rag-tokens | grep INSURER_CREW_RAG_TOKEN | cut -d= -f2)
curl -s -X POST localhost:8400/v1/answer -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"collection":"ins_policy_wording","question":"Is birth control treatment excluded under HEALTH-PLUS-GOLD?","filters":{"policy_product":"HEALTH-PLUS-GOLD","as_of":"2026-09-01"},"top_k":4}'
```

Expect a one-line answer that ends with a citation like `[pw-HPB-v2#p11#s5]`.

---

## 7. Prove the AI agents are correct (a good "trust" moment)

```bash
make check-crew
```

Runs `scripts/check_insurer_crew.py`: eleven hand-decided questions to the real model (a matching person raises no issue, a different
person is flagged, a wrong bill total is reported, a stalling reply stays unresolved, and so on). Needs Ollama; takes about a minute.
Prints `11/11 checks passed`.

---

## 8. Running things one by one (instead of `make e2e-full`)

Each is its own terminal and keeps running. Only needed if you want to click through the **hospital UI**.

| Command | Starts | Port | Main file |
|---|---|---|---|
| `make run-api` | hospital API | 8100 | `hospital/api/app/asgi.py` |
| `make run-crew` | hospital crew | 8010 | `hospital/crew/crew/main.py` |
| `make run-docpipe` | document reader | 8200 | `services/doc-pipeline/docpipe/main.py` |
| `make run-vision` | page quality, stamp detector | 8300 | `services/vision-service/vision/main.py` |
| `make run-ui` | hospital website | 3100 | `hospital/ui/` (Next.js) |
| `make run-insurer-api` | insurer API | 8600 | `insurer/api/insurer_app/main.py` |
| `make run-calc` | payout calculator | 8620 | `insurer/calc_engine/calc_engine/api.py` |
| `make run-tpa-sim` | simulated bank | 8500 | `services/tpa-sim/tpa_sim/main.py` |

Hospital UI: open `http://localhost:3100`. Users are `desk1` (front desk), `officer1` (claims officer), `hadmin` (admin); the password
is the `DEMO_PW` line in `.env` (`grep DEMO_PW .env`). Browser-tested with `make ui-e2e`.

Honest note: the **insurer website** (`insurer/ui`) was built but has not been run in a browser yet. For the insurer side use the
scripted demo above and the SQL views in section 6.

The insurer crew has no plain `make run-` shortcut that works with the real model, because it needs the Ollama settings; `make e2e-full`
and `make check-crew` set them for you (see `scripts/e2e_full.py`, function `main`, the `insurer-crew` block).

---

## 9. Quality commands (for "how do you know it works?")

| Command | Checks | Typical result |
|---|---|---|
| `make test` | hospital, contract, document pipeline, vision, data suites | 463 + 34 + 12 + 63 passed |
| `make test-insurer` | insurer API, calc engine, crew, n8n structure, simulators, RAG | all green |
| `make n8n-test` | the hospital flows inside a real n8n container | 13 passed |
| `make lint` | style/security rules (ruff) | `All checks passed!` |
| `make typecheck` | types on the contract package (the whole workspace is also clean) | `Success: no issues found` |
| `E2E_SCENARIO=all make e2e-full` | the seven stories end to end | `FULL E2E PASSED` |

---

## 10. Stop and reset

| Command | What it does | Careful |
|---|---|---|
| `make down` | stop the shared containers (data kept) | safe |
| `Ctrl+C` in the `make run-rag` terminal | stop RAG | safe |
| `make db-reset` / `make db-reset-insurer` | wipe and recreate one database | deletes that database's data |
| `make nuke` | asks, then deletes **all** volumes | destroys everything; do not use before a demo |

---

## 11. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `address already in use` | that service is already running | `ss -ltnp | grep :<port>`, reuse it or `kill <pid>` (use the pid; avoid `pkill -f`) |
| `... is not reachable ... Run make up-infra / up-n8n` | a container is down | run the matching `make up-...` from section 2 |
| `make e2e-full` waits at the start | another run holds the lock | wait, or delete `.run.lock` only if nothing is running |
| steps are slow | the model is big for the 4 GB GPU, or another job is using Ollama | see "Speed" in `02-HOW-IT-WORKS.md`; stop other Ollama jobs |
| step failed | read the end of `.e2e-logs/<service>.log` (the script also prints the last lines) | |

## 12. Port cheat sheet

| Port | What | | Port | What |
|---|---|---|---|---|
| 3100 | hospital UI | | 8300 | vision service |
| 5452 | hospital Postgres | | 8400 | RAG service |
| 5453 | insurer Postgres | | 8500 | simulated bank (tpa-sim) |
| 5688 | hospital n8n | | 8600 | insurer API |
| 5689 | insurer n8n | | 8610 | insurer crew |
| 6333 | Qdrant (RAG vectors) | | 8620 | calc engine |
| 6390 | Redis | | 9010 / 9011 | MinIO files / console |
| 8010 | hospital crew | | 8080 | Keycloak (login) |
| 8100 | hospital API | | 11434 | Ollama (the model server) |
| 8200 | document pipeline | | | |

Other projects on this machine use 5432, 6379, 8000, 3000, 4000, 5433, 5679, 9000: Vortex deliberately avoids them.
