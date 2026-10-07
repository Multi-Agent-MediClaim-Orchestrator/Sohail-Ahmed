# Vortex — Look inside: the databases, the n8n workflows, and the AI crew

Use this file during a demo to **show** what is happening, not just tell. Everything here is read-only unless it says otherwise.
Start the system with `01-RUNBOOK.md` first. Every command is run from the project folder:

```bash
cd "/home/ashok/Downloads/Multiagent healthcare claim processing"
```

Handy shortcuts for this terminal session (copy-paste once):

```bash
alias hdb='docker exec -it claims-hospital-db-1 psql -U hospital -d hospital'      # hospital database
alias idb='docker exec -it claims-insurer-db-1 psql -U postgres -d insurer'        # insurer database
alias ndb='docker exec -it claims-hospital-db-1 psql -U hospital -d n8n'           # hospital n8n's own database
```

Inside `psql`: `\dt` lists tables, `\d table_name` shows its columns, `\x` switches to one-field-per-line (easier to read wide rows), `\q` quits.
One-off command without entering psql: `hdb -c "select ..."` (drop the `-it` from the alias if a script complains).

---

# PART 1 — The databases

## 1.1 Which database holds what

| Database | Container / port | Holds | Open it |
|---|---|---|---|
| `hospital` | `claims-hospital-db-1` / **5452** | the hospital's cases, patients, documents, drafts, outbox, queries, audit | `hdb` |
| `n8n` | same container | hospital n8n's workflows and run history | `ndb` |
| `insurer` | `claims-insurer-db-1` / **5453** | the insurer's claims, checks, decisions, queries, settlements, config, audit | `idb` |
| Redis | `claims-redis-1` / **6390** | temporary state (idempotency keys, live-event streams) | see 1.5 |
| MinIO | **9010** (console 9011) | the actual PDF/image files | see 1.6 |
| Qdrant | **6333** | RAG vectors (policy knowledge base) | see 1.7 |
| `.e2e-logs/rag.db` | file | RAG bookkeeping (which embedding model, search log) | see 1.8 |

Prefer a GUI (DBeaver, TablePlus)? Use host `localhost`, port **5452** user `hospital` db `hospital` (password: the `HOSP_DB_PASSWORD` line in `.env`),
and port **5453** user `postgres` db `insurer` (password: `INS_PG_SUPERUSER_PASSWORD` in `.env`). Never share `.env`.

## 1.2 Hospital database — the tables that tell the story

| Table | One row is… | Look at it to show |
|---|---|---|
| `claim_case` | one claim | the status moving: draft → … → settled |
| `case_status_history` | one status change | the full timeline of a case |
| `patient`, `insurance_policy_ref` | the patient / the policy numbers | who and which policy |
| `document` | one uploaded file | type, virus-scan result, parse status, **stamp found** |
| `document_parse` | one reading pass of one document | the extracted fields (`typed_json`) and the engine used (`pdftotext+gemma4:latest`) |
| `completeness_check` | one evaluation of "is the file complete?" | `complete`, number of blockers/warnings |
| `requirement_waiver` | a manager waiving a rule | who and why |
| `claim_draft` | one **version** of the built claim | payload (the claim JSON), validation result, who/what produced it |
| `bill_line` | one billed line of a draft | description, category, qty, amount, source document/page |
| `signoff` | an officer's approval of the claim | who signed |
| `outbox` | one message waiting to be / already sent to the insurer | `status` (pending → sent), `attempts`, `sequence` |
| `insurer_query`, `query_response` | the insurer's question / our reply versions | the question text, drafted and approved reply |
| `inbound_callback` | a message received from the insurer | status/decision/settlement updates and their sequence |
| `settlement` | the hospital's copy of the payment | UTR and amount |
| `audit_event`, `case_audit_head` | one audit entry / the latest hash per case | the tamper-evident trail |
| `config_set`, `config_version` | versioned settings (required documents, deadlines, router rules) | what rules applied |
| `app_user`, `hospital`, `network_insurer` | users, the hospital, insurers | master data |

Queries that work (tested on this system):

```sql
-- the latest claims and where they are
select claim_ref, status, claim_type from claim_case order by created_at desc limit 5;

-- the timeline of the latest claim
select from_status, to_status, created_at from case_status_history order by created_at desc limit 10;

-- its documents: type, scan, parse, stamp
select original_filename, doc_type, parse_status, scan_status, has_required_stamp from document order by created_at desc limit 5;

-- what the document reader extracted (first 80 chars of the fields)
select pass_no, engine, confidence, left(typed_json::text, 80) from document_parse order by created_at desc limit 4;

-- was the file complete?
select run_no, complete, blocker_count, warning_count from completeness_check order by created_at desc limit 3;

-- the claim that was built (totals) and whether it had validation errors
select version, has_errors, source, payload->'totals' as totals from claim_draft order by created_at desc limit 3;

-- the outbox: did the claim reach the insurer?
select kind, status, attempts, sequence from outbox order by created_at desc limit 5;

-- questions from the insurer
select status, round, left(text, 60) from insurer_query order by created_at desc limit 5;

-- the audit trail (latest 15 events)
select event_type, actor_type from audit_event order by ts desc limit 15;
```

## 1.3 Insurer database — schema `core` (the business), plus `config`, `audit`, `ops`

| Table | One row is… | Look at it to show |
|---|---|---|
| `core.claim_case` | one claim at the insurer (`IC-2026-…`) | status, claimed vs approved amount, procedure group |
| `core.bill_line`, `core.claim_document` | billed lines / documents received | what was claimed; fetch status of each document |
| `core.policy`, `core.policy_member`, `core.insurance_product` | the insured world (60 policies, 180 members, 3 products) | who is covered, sum insured |
| `core.policy_claim_utilisation` | how much of the sum insured is used | remaining cover |
| `core.network_hospital` | the 12 hospitals (one is blacklisted on purpose) | network status, callback URL |
| `core.verification_run`, `core.verification_step` | one run / one of its six steps | status, **score**, **findings**, and the AI's `agent_output` |
| `core.agent_run` | a recorded agent call | model, prompt version |
| `core.calculation_result` | the calculator's input and output | the full breakdown (`output` JSON) |
| `core.decision` | a recommendation, then the final decision | outcome, amount, deductions, flags |
| `core.decision_task`, `core.approval` | the approval work item / each vote | who approved, their role |
| `core.query`, `core.query_round`, `core.query_response` | questions to the hospital, rounds 1–3, replies | text, status, triage verdict |
| `core.escalation` | a round-3 escalation | reason, who resolved |
| `core.settlement`, `core.settlement_event`, `core.settlement_task` | payments, their events, human follow-ups | status, **UTR**, attempts |
| `config.config_set`, `config.config_version` | versioned insurer rules (thresholds, policy rules, required documents) | the 50,000 / 500,000 limits live here |
| `audit.audit_event`, `audit.case_audit_head` | audit entries / chain head per case | tamper-evident trail |
| `ops.outbox` | messages to hospitals | callbacks sent (`status`, `endpoint`, `attempts`) |
| `ops.idempotency_record` | stored replies for replay protection | |
| Views `core.v_case_list`, `v_gate_stats`, `v_query_aging`, `v_reviewer_load` | ready-made summaries | the reviewer work queue |

```sql
-- the claims and what the insurer decided
select insurer_claim_no, status, claimed_amount, approved_amount from core.claim_case order by created_at desc limit 5;

-- the six verification steps of the newest run, with scores
select step, status, score from core.verification_step order by started_at desc limit 6;

-- the findings (the reasons) of one step as readable rows
select s.step, f->>'code' as code, f->>'severity' as severity, f->>'message' as message
from core.verification_step s, jsonb_array_elements(s.findings) f
order by s.started_at desc limit 10;

-- recommendation vs final decision
select kind, outcome, approved_amount, status, flags from core.decision order by created_at desc limit 4;

-- who approved
select approver, verdict, approver_role from core.approval order by created_at desc limit 5;

-- the questions and their rounds
select round, status, origin, left(text, 60) from core.query order by created_at desc limit 5;

-- payments
select status, utr, amount, attempt_count from core.settlement order by created_at desc limit 5;

-- messages sent back to hospitals
select kind, status, attempts, endpoint from ops.outbox order by created_at desc limit 8;

-- the thresholds the gate uses
select s.domain, s.name, v.version, v.status, v.payload->>'t_auto_inr' as t_auto, v.payload->>'t_four_inr' as t_four
from config.config_set s join config.config_version v on v.config_set_id = s.id where s.domain = 'thresholds';

-- the reviewer queue as the UI sees it
select * from core.v_case_list limit 5;
```

## 1.4 Showing the AI's work inside the database (the crew's saved answers)

The insurer stores each agent's output as JSON in `core.verification_step.agent_output`:

```sql
-- which agents ran on the newest claim, which model served them, any warnings
select step,
       agent_output->>'model_alias'    as model,
       agent_output->>'prompt_version' as prompt,
       agent_output->>'degraded'       as degraded,
       agent_output->>'warnings'       as warnings
from core.verification_step where agent_output is not null order by started_at desc limit 4;

-- the identity agent's notes, in full
select jsonb_pretty(agent_output) from core.verification_step
where step = 'identity' and agent_output is not null order by started_at desc limit 1;

-- the coverage agent's cited clauses
select jsonb_pretty(agent_output->'citations') from core.verification_step
where step = 'coverage' and agent_output is not null order by started_at desc limit 1;
```

Rows where `agent_output` is empty ran without the crew (the model-free mode) — only the code decided.

## 1.5 Redis (temporary state)

```bash
docker exec claims-redis-1 redis-cli --user admin --no-auth-warning --scan --pattern '*' | head          # list keys
docker exec claims-redis-1 redis-cli --user admin --no-auth-warning xlen sse:insurer:events               # how many live events so far
docker exec claims-redis-1 redis-cli --user admin --no-auth-warning --scan --pattern 'idem:hosp:*' | wc -l  # replay-protection keys
```

Key families: `idem:hosp:…` / `idem:ins:…` (idempotency), `cache:hosp:…` (cached profiles), `sse:hospital:…` / `sse:insurer:…` (live-event streams), `rl:…` (rate limits).

## 1.6 MinIO (the actual files)

Open `http://localhost:9011`; sign in with `SHARED_MINIO_ROOT_USER` and `SHARED_MINIO_ROOT_PASSWORD` from `.env`. Buckets: `hospital-docs`
(uploaded PDFs, plus rendered page previews under `…/pages/N.png`), `insurer-docs` (what the insurer fetched), `kb-sources`.
The `document.storage_key` column in the hospital database is the object path.

## 1.7 Qdrant (RAG vectors)

```bash
KEY=$(grep ^QDRANT_API_KEY .env | cut -d= -f2)
curl -s -H "api-key: $KEY" localhost:6333/collections | python3 -m json.tool
curl -s -H "api-key: $KEY" localhost:6333/collections/ins_policy_wording | python3 -c "import sys,json;r=json.load(sys.stdin)['result'];print('chunks:',r['points_count'])"
# look at three stored chunks (the text, the product, the version, the effective dates)
curl -s -X POST -H "api-key: $KEY" -H "Content-Type: application/json" localhost:6333/collections/ins_policy_wording/points/scroll \
  -d '{"limit":3,"with_payload":["policy_product","version","effective_from","effective_to","section","text"],"with_vector":false}' | python3 -m json.tool | head -40
```

Four collections (each is an alias to a `…__v1` collection): `ins_policy_wording`, `ins_medical_guidelines`, `hosp_insurer_rules`, `ins_case_history`.

## 1.8 RAG bookkeeping (SQLite)

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect(".e2e-logs/rag.db")
for t in ("collection_meta", "retrieval_log", "ingest_job"):
    print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
for row in c.execute("select collection, embed_model, embed_dim from collection_meta"): print(row)
PY
```
(If a RAG service started before the Makefile path fix wrote to `services/rag-service/.e2e-logs/rag.db`, look there; a restart uses the
shared path above.)

---

# PART 2 — Watching the workflows in n8n (in the browser)

n8n has a visual editor: each workflow is boxes (nodes) joined by lines. **We never build flows by hand** — they are generated from code
(`hospital/n8n/build_flows.py`, `insurer/n8n/build_flows.py`) so they stay reviewable. The browser is for *watching*.

## 2.1 Open them

| Side | Address | Container | Workflows |
|---|---|---|---|
| Hospital | `http://localhost:5688` | `claims-hospital-n8n-1` | 13 (intake, completeness, claim build, submission watch, query intake/draft/SLA, reminders, sweeper, error, 3 utilities) |
| Insurer | `http://localhost:5689` | `claims-insurer-n8n-1` | 19 (verification main + 6 step flows, decision gate/final, query sent/response, escalation, settlement, cron, 4 helpers) |

The first time n8n may ask you to **create an owner account** (any e-mail and password; it lives only on this machine's n8n database).
Both servers are bound to this laptop only.

## 2.2 What to click

1. **Workflows** (left menu) → the list. Names: hospital `hosp-intake-document`, `hosp-claim-build`, …; insurer `10_verification_main`, `11_step_document_fetch` … `16_step_calculation`, `50_settlement`.
2. Click `hosp-intake-document` (hospital) or `10_verification_main` (insurer): you see the nodes. The first node is the **trigger** (a Webhook box, or a clock for cron flows).
3. **Executions** (top tab inside a workflow, or *Executions* in the left menu) → every run, with a green/red status. Click one to see the data that passed through each node.
4. Webhook URLs (what the API calls): hospital `http://localhost:5688/webhook/intake/document-uploaded`, `…/claim/submitted`, `…/query/intake`;
   insurer `http://localhost:5689/webhook/verification-start`, `…/decision-final`, `…/settlement-initiate`. They require the secret header, so opening them in a browser gives an error — that is correct.
5. The **error handler** flow (`hosp-global-error`, `00_common_error_handler`) fires when any flow fails.

## 2.3 What you will see in *Executions*

- **Both n8n servers keep every run** (successful and failed). Hospital runs are cleaned after 14 days, insurer runs after 7 days.
  After a demo you will see, for the insurer, one `10_verification_main` run per claim and one sub-flow run for each of its six steps
  (`11_step_document_fetch` … `16_step_calculation`), plus `03_call_crew` runs when the crew is on; click a run to see the data passing through each node.
- The runs carry claim ids and masked data only (the flows strip payloads before raising alerts); everything is synthetic anyway.
- The same story is always readable from the database: `core.verification_run` and `core.verification_step` (section 1.3) show every step the
  n8n flow executed, because the API records each one.

## 2.4 Read the same thing without a browser

```bash
# hospital workflows and whether they are active
ndb -c "select name, active from workflow_entity order by name"
# the last runs of the hospital flows
ndb -c "select w.name, e.status, e.\"startedAt\" from execution_entity e join workflow_entity w on w.id = e.\"workflowId\" order by e.\"startedAt\" desc limit 10"
# n8n's own log (shows imports, errors)
docker logs --tail 40 claims-insurer-n8n-1
docker logs --tail 40 claims-hospital-n8n-1
```

Where the flows come from: JSON files in `hospital/n8n/flows/` and `insurer/n8n/flows/`; at container start `entrypoint.sh` imports them
(`n8n import:workflow`) and publishes them. Changing a flow means changing the generator, then `make flows` (hospital) or
`uv run python insurer/n8n/build_flows.py` (insurer), then restarting the container (`make up-n8n` / `make up-insurer-n8n`).

---

# PART 3 — Running and watching the crew (the AI agents)

## 3.1 The quickest way: the built-in check (starts the crew itself)

```bash
make check-crew
```
It starts the insurer crew on port 8610, sends eleven hand-made cases to the real model, prints PASS/FAIL with the time of each, and stops the crew.
Code: `scripts/check_insurer_crew.py`. With the RAG service running it also checks the coverage agent against the real knowledge base; without it, it checks
the coverage agent says "insufficient evidence" instead of inventing clauses. To run only some checks: `CHECK_ONLY=identity make check-crew`.
To try another model: `INS_CREW_MODEL=granite4.1:3b make check-crew` (the same eleven checks tell you if it is good enough).

## 3.2 See it work inside a full claim

```bash
E2E_ORCH=n8n E2E_CREW=ollama E2E_SCENARIO=reviewer make e2e-full
```
While it runs, in another terminal:

```bash
tail -f .e2e-logs/insurer-crew.log        # the crew's own log
tail -f .e2e-logs/insurer-api.log         # the API calling it
```
Afterwards use the SQL in section 1.4 to read what each agent said about the claim.

## 3.3 Run the insurer crew by hand and talk to it

Terminal 1 — start the crew (leave running). The model and RAG settings are what `scripts/e2e_full.py` passes it:

```bash
TOK=$(make -s rag-tokens | grep INSURER_CREW_RAG_TOKEN | cut -d= -f2)      # a token for the RAG service
cd insurer/crew
INS_LLM_GATEWAY_URL=http://localhost:11434 INS_LLM_VIRTUAL_KEY=ollama \
INS_ALIAS_SMART=gemma4:latest INS_ALIAS_FAST=gemma4:latest INS_ALIAS_FALLBACK=gemma4:latest \
INS_LLM_REASONING_EFFORT=none INS_CREW_SERVICE_TOKENS=demo \
INS_RAG_URL=http://localhost:8400 INS_RAG_TOKEN=$TOK \
uv run uvicorn insurer_crew.main:app --port 8610
```

| Setting | Meaning |
|---|---|
| `INS_LLM_GATEWAY_URL=http://localhost:11434` | the model server (Ollama) |
| `INS_ALIAS_*` | which model name each role uses |
| `INS_LLM_REASONING_EFFORT=none` | no "thinking" → faster answers |
| `INS_CREW_SERVICE_TOKENS=demo` | the password callers must send in `X-Service-Token` |
| `INS_RAG_URL`, `INS_RAG_TOKEN` | where the policy knowledge base is and the token to read it |

Terminal 2 — ask it things:

```bash
# which agents does it have, with which model and prompt version?
curl -s -H "X-Service-Token: demo" localhost:8610/v1/agents | python3 -m json.tool

# is the crew healthy (and can it reach the model and RAG)?
curl -s localhost:8610/v1/health

# the identity agent: a different person must be flagged
curl -s -X POST localhost:8610/v1/identity/analyze -H "X-Service-Token: demo" -H "Content-Type: application/json" -d '{
  "request_id":"11111111-1111-4111-8111-111111111111","case_id":"demo-1",
  "context":{
    "member":{"member_id":"MEM-****0003","full_name":"Rohan Iyer","dob":"2016-09-25","gender":"M"},
    "patient":{"full_name":"Suresh Verma","dob":"1979-03-02","gender":"M"},
    "deterministic_facts":{"name_score":0.12,"dob_match":false,"policy_match":true,"member_found":true},
    "docs":[{"doc_id":"d1","doc_type":"id_proof","extract_masked":{"patient_name":"Suresh Verma","dob":"1979-03-02"}}]}}' | python3 -m json.tool

# the coverage agent with the knowledge base: expect a cited clause (the quote comes straight from the policy wording)
curl -s -X POST localhost:8610/v1/coverage/analyze -H "X-Service-Token: demo" -H "Content-Type: application/json" -d '{
  "request_id":"22222222-2222-4222-8222-222222222222","case_id":"demo-2",
  "context":{"policy":{"product_code":"HEALTH-BASIC"},"diagnosis_codes":["K80.20"],"procedure_codes":["Cholecystectomy"],"admitted_on":"2026-09-02"}}' | python3 -m json.tool
```
(Ten more ready-made inputs, one per agent, are inside `scripts/check_insurer_crew.py` — copy any of them.)

What to point out in an answer: `degraded: false`, `model_alias`, `prompt_version` (which prompt file), `trace_id`, and for coverage the
`citations` with `chunk_id` such as `pw-HPA-v3#p8#s4.1.2` and a verbatim `quote`.

## 3.4 Where the crew's pieces are (to open in the editor while you explain)

| To explain… | Open |
|---|---|
| the agent's instructions | `insurer/crew/insurer_crew/prompts/identity-v1.md` (and the other six) |
| the agent's code | `insurer/crew/insurer_crew/agents.py` — function `identity`, `authenticity`, `coverage`, … |
| the strict answer shape | `insurer/crew/insurer_crew/schemas.py` |
| how the model is called and retried | `insurer/crew/insurer_crew/runtime.py` — class `GatewayLLM` and the runner |
| the safety net | `insurer/crew/insurer_crew/validators.py` |
| the web layer | `insurer/crew/insurer_crew/app.py` |
| who calls the crew | `insurer/api/insurer_app/clients/crew.py`, `services/orchestrator.py` (`_agent_body`), `insurer/n8n/build_flows.py` (`03_call_crew`) |

## 3.5 The hospital crew

```bash
make run-crew                                  # real model; port 8010
CREW_LLM=rules make run-crew                   # deterministic, no model at all (fast)
curl -s localhost:8010/v1/health
curl -s localhost:8010/v1/jobs | python3 -m json.tool       # recent jobs (claim-build, query triage, query draft)
```
The hospital API starts its jobs (`hospital/api/app/services/crew.py`) and polls them; you rarely call it by hand. Open `hospital/crew/crew/agents/builder.py`
(claim builder) and `responder.py` (query replies) to explain it; `guards/` holds the personal-data and grounding checks. Model setting:
`HOSP_LLM_LOCAL_MODEL` (default `gemma4:latest`).

## 3.6 The RAG service by hand

```bash
curl -s localhost:8400/health
TOK=$(make -s rag-tokens | grep INSURER_CREW_RAG_TOKEN | cut -d= -f2)
# raw search: the chunks, their scores and citation ids
curl -s -X POST localhost:8400/v1/search -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"collection":"ins_policy_wording","query":"room rent limit","filters":{"policy_product":"HEALTH-BASIC","as_of":"2026-09-01"},"top_k":3}' | python3 -m json.tool | head -40
# cited answer (about 3-9 seconds)
curl -s -X POST localhost:8400/v1/answer -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"collection":"ins_policy_wording","question":"Is birth control treatment excluded under HEALTH-PLUS-GOLD?","filters":{"policy_product":"HEALTH-PLUS-GOLD","as_of":"2026-09-01"},"top_k":4}'
```
A nice demo of **dates matter**: ask the same room-rent question with `"as_of":"2025-09-01"` and `"2026-09-01"` for `HEALTH-BASIC` — you get different wording
(version 1 vs version 3) with different percentages, because only the wording in force on the admission date may be used.

---

# Quick map: "I want to show … → do this"

| Show | Do |
|---|---|
| a claim moving through statuses | run `E2E_SCENARIO=reviewer make e2e-full`, then section 1.2 first query and 1.3 first query |
| the checks the insurer ran | 1.3 "verification steps" and "findings" queries |
| what the AI said, and that it is cited | 1.4 queries; 3.3 coverage `curl` |
| the workflow engine | 2.2 (browser), or 2.4 (terminal) |
| nothing is lost on a crash | 1.2 outbox query (`sent`, `attempts`); runbook scenario `callbacks` |
| the money is simulated | 1.3 settlement query (`SIMUTR…` UTR) and `services/tpa-sim/` |
| the audit chain | 1.2/1.3 audit queries; the last step of `e2e-full` verifies both chains |
