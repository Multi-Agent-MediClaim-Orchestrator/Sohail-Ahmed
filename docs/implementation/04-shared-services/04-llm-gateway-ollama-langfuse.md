# 04-04 — llm-gateway (LiteLLM), Ollama, Langfuse

Owner: **Dev B**. Ports: LiteLLM 4000, Ollama 11434, Langfuse 3200. Status: PROPOSED where marked.

Related docs: `01-shared-contract/05-repo-layout-and-conventions.md` (env, compose profiles), `01-shared-contract/04-audit-hash-chain.md` (model_info fields in audit events), `04-shared-services/01-infra-redis-minio-clamav-keycloak.md` (Redis), `05-integration-and-eval/03-privacy-and-security.md` (PII routing), `05-integration-and-eval/04-observability-and-runbook.md` (trace conventions).

---

## 1. Goal

Architecture rule: **every LLM call goes through LiteLLM; no service except the gateway holds a provider key.** This document specifies the gateway, the local model runtime (Ollama), and the observability backend (Langfuse) as one deliverable because they are operated, configured and debugged together.

What the gateway gives the project:

| Capability | Why it matters here |
|---|---|
| Model aliases (`reason-cloud`, `cleanup-local`, ...) | Crews and pipelines code against a purpose, never a vendor model name. Swapping Gemini Flash for another model is a config change. |
| Routing + fallback | Free-tier quotas are small (assume ~10 requests/min). When the cloud model returns 429 the call falls back to a local Ollama model instead of failing a claim. |
| Response caching | Evaluation runs replay the same prompts; deterministic calls are cached in Redis so repeated eval runs cost nothing and are reproducible. |
| Virtual keys, rate limits, budgets | One key per caller service. A runaway loop in one crew cannot starve the others or burn the whole daily quota. |
| PII guard | Second line of defence behind Presidio: cloud aliases reject text that still looks like an Aadhaar/PAN/phone number. |
| Tracing | Every call is a Langfuse generation tagged with `system`, `agent`, `claim_ref`, `prompt_version`, giving cost and latency per claim (architecture §3, Langfuse row). |

Non-goals: the gateway does not implement agent logic, prompt content, JSON repair or retries of business-level validation. Those live in the crews (docs `02-dev-A-hospital/09-crew-agents.md`, `03-dev-B-insurer/08-crew-agents.md`).

---

## 2. Inputs / Outputs

### 2.1 Inputs
- OpenAI-compatible HTTP requests to `http://llm-gateway:4000/v1/*` with header `Authorization: Bearer <virtual-key>`.
- Request `metadata` object carrying correlation fields (section 5.2).
- Admin API calls (`/key/generate`, `/key/update`, `/spend/*`) authenticated with the master key; used only by `init_keys.sh` and ops scripts.

### 2.2 Outputs
- OpenAI-format completions/embeddings.
- Response headers: `x-litellm-model-id`, `x-litellm-response-cost`, `x-embed-model` (PROPOSED custom header for embeddings, section 8), `x-llm-fallback-used: true|false` (PROPOSED custom success callback adds it).
- Langfuse traces/generations.
- Prometheus metrics at `/metrics` (LiteLLM built-in, enabled via `litellm_settings.callbacks: ["prometheus"]`).

### 2.3 Callers (virtual keys)

| Virtual key name | Service | Allowed aliases | rpm | Daily token budget (PROPOSED) |
|---|---|---|---|---|
| `hospital-crew` | hospital-crew | `reason-cloud`, `reason-local`, `cleanup-local` | 20 | 400k |
| `insurer-crew` | insurer-crew | `reason-cloud`, `reason-local` | 30 | 600k |
| `doc-pipeline` | doc-pipeline | `cleanup-local`, `cleanup-cloud` | 30 | 500k |
| `vision-service` | vision-service | `vision-cloud` | 10 | 100k |
| `rag-service` | rag-service | `embed`, `reason-cloud`, `reason-local` | 60 | 500k |
| `n8n-hospital` | hospital-n8n | `reason-local` | 10 | 50k |
| `n8n-insurer` | insurer-n8n | `reason-local` | 10 | 50k |
| `eval-harness` | evaluation harness | all | 40 | 1M (separate budget) |
| `tpa-sim` | tpa-sim (optional LLM mode) | `reason-local` | 10 | 50k |

The sum of per-key rpm intentionally exceeds the provider's real rpm; the **router-level** rpm on the cloud deployment (section 4.2) is the real limiter, and fallbacks absorb the excess.

---

## 3. Alias catalogue (PROPOSED)

| Alias | Primary | Fallback chain | Context window | Use | Receives PII? |
|---|---|---|---|---|---|
| `cleanup-local` | `ollama/llama3.1:8b` (quantised, e.g. Q4) | `ollama/llama3.2:3b` | 8k | doc-pipeline pass A (cleanup of parsed text) | Masked text only (Presidio already ran) |
| `cleanup-cloud` | `gemini/gemini-flash-lite` | `cleanup-local` | 32k | doc-pipeline pass B (agreement check) | Masked text only; PII guard enforced |
| `reason-cloud` | `gemini/gemini-flash` | `reason-local` | 32k | coverage reasoning, query drafts, supervisors | Masked/structured data only; PII guard enforced |
| `reason-local` | `ollama/llama3.1:8b` | none | 8k | offline mode, n8n helpers, tpa-sim | May receive unmasked internal text because it never leaves the machine |
| `vision-cloud` | `gemini/gemini-flash` (image input) | none (caller degrades to `needs_human`) | image + text | stamp/signature escalation | Cropped stamp regions only, never full pages |
| `embed` | `ollama/nomic-embed-text` (768d) | `ollama/bge-small-en` (384d, flagged) | 2k tokens | RAG embedding | Policy text and masked case text |

Rules:
1. Aliases containing `cloud` are the only ones that may call external providers. This naming rule is relied on by the PII guard and by `LLM_MODE=local`.
2. Fallback from `embed` to a different dimensionality is **disabled by default** (a 384d vector cannot go in a 768d collection). The fallback exists only for health-check use; production config sets `embed` fallbacks to `[]`.
3. Model names inside `litellm_params` are the only place vendor names appear. Docs and code elsewhere use aliases.
4. Check provider quotas and model availability at build time (free tiers change). Record the date checked in `infra/llm-gateway/QUOTAS.md`.

---

## 4. Configuration

### 4.1 File layout

```
infra/llm-gateway/
  Dockerfile
  config.yaml                 # main LiteLLM config (section 4.2)
  config.local.yaml           # LLM_MODE=local overlay (section 7.4)
  .env.example
  callbacks/
    __init__.py
    pii_guard.py              # pre-call hook
    fallback_header.py        # success hook adding x-llm-fallback-used
    embed_header.py           # adds x-embed-model
  scripts/
    init_keys.sh
    pull_models.sh
    warmup.sh
    smoke.sh
    cost_by_claim.py
  TRACING.md
  QUOTAS.md
  tests/
infra/langfuse/
  docker-compose.langfuse.yml   # or entries in compose/shared.yml
  .env.example
infra/ollama/
  Modelfile.llama3.2-3b-json    # optional tuned defaults
```

### 4.2 `config.yaml` (full)

```yaml
model_list:
  # ---------- local (Ollama) ----------
  # User decision: very weak GPU. Default is Llama 3.1 8B quantised; fall back to Llama 3.2 3B.
  - model_name: cleanup-local
    litellm_params:
      model: ollama/llama3.1:8b
      api_base: http://ollama:11434
      timeout: 180
      keep_alive: 10m
    model_info: {id: cleanup-local-8b}

  - model_name: cleanup-local-fallback
    litellm_params:
      model: ollama/llama3.2:3b
      api_base: http://ollama:11434
      timeout: 120
      keep_alive: 10m
    model_info: {id: cleanup-local-3b}

  - model_name: reason-local
    litellm_params:
      model: ollama/llama3.1:8b
      api_base: http://ollama:11434
      timeout: 180
      keep_alive: 10m
    model_info: {id: reason-local-8b}

  - model_name: embed
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://ollama:11434
      timeout: 60
    model_info: {id: embed-nomic, mode: embedding}

  # ---------- cloud (Gemini) ----------
  - model_name: reason-cloud
    litellm_params:
      model: gemini/gemini-flash-latest
      api_key: os.environ/GEMINI_API_KEY
      rpm: 10             # real provider limit (free tier; adjust per QUOTAS.md)
      tpm: 200000
      timeout: 60
    model_info: {id: reason-cloud-flash}

  - model_name: cleanup-cloud
    litellm_params:
      model: gemini/gemini-flash-lite-latest
      api_key: os.environ/GEMINI_API_KEY
      rpm: 15
      tpm: 250000
      timeout: 45
    model_info: {id: cleanup-cloud-lite}

  - model_name: vision-cloud
    litellm_params:
      model: gemini/gemini-flash-latest
      api_key: os.environ/GEMINI_API_KEY
      rpm: 5
      tpm: 100000
      timeout: 90
    model_info: {id: vision-cloud-flash, supports_vision: true}

router_settings:
  routing_strategy: simple-shuffle        # one deployment per alias, so effectively direct
  num_retries: 2
  retry_after: 2
  allowed_fails: 3                        # cooldown a deployment after 3 failures
  cooldown_time: 30
  timeout: 60
  redis_host: redis
  redis_port: 6379
  redis_password: os.environ/LLM_REDIS_PW
  fallbacks:
    - {"reason-cloud": ["reason-local"]}
    - {"cleanup-cloud": ["cleanup-local"]}
    - {"cleanup-local": ["cleanup-local-fallback"]}
  context_window_fallbacks:
    - {"cleanup-local": ["cleanup-local-fallback"]}
    - {"reason-local": ["reason-cloud"]}   # only when LLM_MODE != local; stripped by overlay

litellm_settings:
  drop_params: true                       # silently drop params a provider does not support
  set_verbose: false
  json_logs: true
  cache: true
  cache_params:
    type: redis
    host: redis
    port: 6379
    password: os.environ/LLM_REDIS_PW
    namespace: "llmcache"
    ttl: 86400
    supported_call_types: ["acompletion", "completion", "embedding", "aembedding"]
  success_callback: ["langfuse", "prometheus"]
  failure_callback: ["langfuse", "prometheus"]
  callbacks: ["callbacks.pii_guard.proxy_handler_instance",
              "callbacks.fallback_header.proxy_handler_instance",
              "callbacks.embed_header.proxy_handler_instance"]
  langfuse_default_tags: ["gateway"]
  turn_off_message_logging: false         # prompts are masked; Langfuse is local-only
  request_timeout: 120

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/LITELLM_DB_URL # schema `litellm` in a small Postgres (keys, spend)
  allow_requests_on_db_unavailable: true  # fail-open for already-cached keys
  alerting: []                            # alerts handled in 05-integration/04 runbook via Prometheus
  max_parallel_requests: 40
  global_max_parallel_requests: 60
  disable_spend_logs: false
```

Notes on choices:
- `cleanup-local-fallback` is a separate `model_name` so the fallback list can reference it without recursion.
- `max_parallel_requests` is deliberately low: a CPU-only Ollama cannot serve more; excess requests queue at the gateway and the caller sees latency rather than errors.
- `keep_alive: 10m` pairs with `OLLAMA_MAX_LOADED_MODELS=1` (section 6, task 2). With only one model resident, alternating between 3B and 8B models triggers reloads (20-60 s). Mitigation in section 8.

### 4.3 Environment variables (`infra/llm-gateway/.env.example`)

```
GEMINI_API_KEY=                  # ONLY place a provider key may appear
LITELLM_MASTER_KEY=sk-master-change-me
LITELLM_DB_URL=postgresql://litellm:litellm@llm-gateway-db:5432/litellm
LLM_REDIS_PW=change-me
LLM_MODE=hybrid                  # hybrid | local
LANGFUSE_HOST=http://langfuse:3000
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
OLLAMA_KEEP_ALIVE=10m
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
PII_GUARD_MODE=enforce           # enforce | log
```

### 4.4 Virtual-key bootstrap (`scripts/init_keys.sh`)

Pseudocode (bash + curl + jq):

```bash
create_key() {  # name, aliases(json array), rpm, max_budget_tokens
  curl -sS -X POST "$GW/key/generate" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
    -d "{
      \"key_alias\": \"$1\",
      \"models\": $2,
      \"rpm_limit\": $3,
      \"max_parallel_requests\": 5,
      \"metadata\": {\"service\": \"$1\"},
      \"budget_duration\": \"1d\",
      \"max_budget\": null,
      \"tpm_limit\": $4
    }" | jq -r .key
}
```
Notes:
- LiteLLM budgets are cost-based (USD). Free-tier models report zero cost, so the **token budget is enforced through `tpm_limit` and a daily token counter** implemented in `callbacks/token_budget.py` (PROPOSED; reads Redis key `tokbudget:{key_alias}:{YYYYMMDD}`, increments after each call, raises 429 `budget_exceeded` when exceeded). This is the reason the budget column in section 2.3 is tokens.
- The script is idempotent: it looks up an existing key by `key_alias` (`/key/info`) and updates instead of recreating. Generated keys are written to `infra/llm-gateway/.keys.env` (gitignored) and sourced by compose via `env_file`.
- Keys are rotated with `init_keys.sh --rotate <alias>`; the old key is deleted after the new one is written.

---

## 5. API

### 5.1 Endpoints (OpenAI-compatible)

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | main call; supports `response_format` (json_schema / json_object), `tools` (not used in v1) |
| POST | `/v1/embeddings` | alias `embed`; input string or list; returns 768d |
| GET | `/v1/models` | lists aliases allowed for the caller's key |
| GET | `/health/liveliness` | process alive |
| GET | `/health/readiness` | DB + Redis + callback load |
| GET | `/health` | deployment health (calls each model; slow; ops only, master key) |
| GET | `/metrics` | Prometheus |
| POST | `/key/generate`, `/key/update`, `/key/delete`, `/key/info` | master key only |
| GET | `/spend/logs`, `/spend/tags` | master key only |

### 5.2 Required request metadata

Every caller must send:

```json
{
  "model": "reason-cloud",
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "temperature": 0,
  "response_format": {"type": "json_schema", "json_schema": {"name": "CoverageFinding", "schema": {"type": "object"}, "strict": true}},
  "metadata": {
    "system": "insurer",
    "claim_ref": "HC-2026-000123",
    "agent": "coverage",
    "prompt_version": "coverage@3",
    "trace_id": "0af7651916cd43dd8448eb211c80319c",
    "session_id": "HC-2026-000123",
    "no_cache": false,
    "tags": ["stage:verification"]
  }
}
```

Metadata validation (in `pii_guard.py` pre-call hook, same file for simplicity, PROPOSED):
- `system` must be `hospital|insurer|shared`; `agent` and `prompt_version` required for keys other than `eval-harness` and n8n keys. Missing → 400 `metadata_required`.
- `claim_ref` optional for non-claim calls (e.g. KB ingestion); if missing then `session_id` is set to `kb-ingest`.

### 5.3 Error model

Gateway errors are mapped to the contract's RFC 7807 shape only where the gateway is exposed to humans. For service-to-service calls it returns OpenAI-style errors; client libraries in crews convert them. Codes:

| HTTP | `error.code` | Meaning | Caller behaviour |
|---|---|---|---|
| 400 | `pii_detected` | cloud alias received text matching a PII pattern | Do not retry; fix masking; log incident (runbook R-PII) |
| 400 | `metadata_required` | missing required metadata | Programming error |
| 401 | `invalid_key` | bad/rotated virtual key | Alert |
| 403 | `model_not_allowed` | alias not in key's allow-list | Programming error |
| 429 | `rate_limited` | key rpm/tpm exceeded | Backoff 2^n, max 3 |
| 429 | `budget_exceeded` | daily token budget | Degrade to `needs_human` |
| 502 | `provider_error` | all fallbacks failed | Degrade to `needs_human` |
| 504 | `timeout` | upstream slow | One retry then degrade |

### 5.4 Example calls

Chat (structured output on cloud alias):
```bash
curl -s http://llm-gateway:4000/v1/chat/completions \
  -H "Authorization: Bearer $INSURER_CREW_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"reason-cloud","temperature":0,
       "messages":[{"role":"user","content":"Summarise the exclusion clause in two lines."}],
       "metadata":{"system":"insurer","agent":"coverage","prompt_version":"coverage@3","claim_ref":"HC-2026-000123"}}'
```

Embedding:
```bash
curl -s http://llm-gateway:4000/v1/embeddings \
  -H "Authorization: Bearer $RAG_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"embed","input":["room rent cap clause","ICU sub-limit"],"metadata":{"system":"shared","agent":"rag-ingest","prompt_version":"n/a"}}'
```

---

## 6. Build tasks

Each task lists files and a verification command.

1. **Scaffold** `infra/llm-gateway/`: `Dockerfile` (`FROM ghcr.io/berriai/litellm:main-stable`; copy `config.yaml`, `callbacks/`; `CMD ["--config","/app/config.yaml","--port","4000"]`), `.env.example`, `QUOTAS.md` stub. Verify: `docker build`.
2. **Compose entries** in `infra/compose/shared.yml` (profile `llm`):
   - `llm-gateway`: depends_on `llm-gateway-db`, `redis`, `ollama`; env_file; healthcheck `curl -f http://localhost:4000/health/liveliness`; `mem_limit: 1g`.
   - `llm-gateway-db`: `postgres:16-alpine`, volume `llm_gateway_pg`, `mem_limit: 256m`.
   - `ollama`: image `ollama/ollama`, volume `ollama_models:/root/.ollama`, `mem_limit: 6g`, env `OLLAMA_KEEP_ALIVE=10m`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`; optional GPU block commented out.
   - `langfuse` + `langfuse-db` (Postgres) (+ ClickHouse/Redis if the chosen Langfuse major requires them; see 6.6 note). Ports `3200:3000`.
   Verify: `docker compose --profile llm up -d && make llm-health`.
3. **`scripts/pull_models.sh`**: `ollama pull llama3.2:3b; ollama pull llama3.1:8b; ollama pull nomic-embed-text`. First run downloads several GB; document expected disk (≈ 9 GB) and time. Skip-if-present logic via `ollama list`. Verify: `ollama list` shows three models.
4. **`scripts/init_keys.sh`** per 4.4 plus `callbacks/token_budget.py`. Verify: calling an unauthorised alias returns 403.
5. **Callbacks**: `pii_guard.py` (6.1), `fallback_header.py`, `embed_header.py`, `token_budget.py`. Verify: unit tests (section 9).
6. **Langfuse bootstrap**: env `LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_PROJECT_ID`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY`, `LANGFUSE_INIT_USER_EMAIL`, `LANGFUSE_INIT_USER_PASSWORD`, `NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`, `TELEMETRY_ENABLED=false`, `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES=false`. Note: Langfuse v3 needs ClickHouse, Redis and S3-compatible blob storage; to stay inside 16 GB RAM, pin **Langfuse v2** (single container + Postgres) unless the team opts in to v3 (PROPOSED: v2; recorded in `infra/langfuse/README.md`). Verify: login at `http://localhost:3200`, API keys match the gateway env.
7. **Prompt registry convention** (7.5): create prompt names in Langfuse, local fallbacks in each crew repo `prompts/<agent>.<version>.md`.
8. **Trace conventions doc** `TRACING.md` (section 7.6).
9. **Warm-up**: `scripts/warmup.sh` sends a 5-token prompt per local model and an embed call; run as a compose one-shot `llm-warmup` after gateway healthy. Verify: first real request latency < 5 s.
10. **Smoke + cost scripts**: `smoke.sh` (every alias hello), `cost_by_claim.py` (Langfuse API: group generations by `session_id`, sum tokens, latency p50/p95 per agent, output CSV and markdown).
11. **LLM_MODE overlay** (7.4) and `make llm-local`.
12. **CI grep check**: `scripts/ci_no_provider_keys.sh` fails when `GEMINI_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY` appears in any compose env/`.env.example` outside `infra/llm-gateway/`.
13. **Tests** per section 9.

### 6.1 `pii_guard.py` (pseudocode)

```python
import re
from litellm.integrations.custom_logger import CustomLogger

PATTERNS = {
    "aadhaar": re.compile(r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "mobile": re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "member_id_raw": re.compile(
        r"\bMEM-\d{8,}\b"
    ),  # raw IDs must never leave; masked form is MEM-****1234
}
ALLOW_LISTED_TOKENS = re.compile(r"<(PERSON|PHONE|ID|EMAIL|ADDRESS)_\d+>")  # Presidio placeholders


class PIIGuard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model", "")
        self._check_metadata(data, user_api_key_dict)
        if "cloud" not in model:  # local aliases are exempt
            return data
        text = self._flatten(
            data
        )  # messages + tool args + image captions; ignores base64 image bytes
        text = ALLOW_LISTED_TOKENS.sub("", text)
        hits = {k for k, p in PATTERNS.items() if p.search(text)}
        if hits and MODE == "enforce":
            audit_log(
                "pii_blocked",
                key=user_api_key_dict.key_alias,
                hits=sorted(hits),
                claim_ref=meta.get("claim_ref"),
            )
            raise HTTPException(
                400, {"error": {"code": "pii_detected", "message": f"patterns: {sorted(hits)}"}}
            )
        return data
```
Important details:
- The guard logs **pattern names only, never the matched text**.
- Luhn-like false positives: 12-digit numbers that are bill amounts. Guard requires Aadhaar first digit 2-9 and rejects only when the number is not preceded by a currency symbol or `INR`/`Rs`. Known limitation recorded in section 8.
- `LLM_MODE=local` rewrites aliases before this hook, so no PII check is needed in that mode.

### 6.2 `fallback_header.py`

On success, compares `kwargs["model"]` (requested alias) with `response._hidden_params["model_id"]`; if different sets `x-llm-fallback-used: true` and writes Langfuse tag `fallback:<from>-><to>` so the eval harness can report model mix (05-integration/02).

### 6.3 `embed_header.py`

Adds `x-embed-model: nomic-embed-text@768` to embedding responses; rag-service stores this string in collection metadata (doc 05 section 3).

### 6.4 `token_budget.py`

```python
async def async_pre_call_hook(...):
    key = user_api_key_dict.key_alias
    used = int(await redis.get(f"tokbudget:{key}:{today()}") or 0)
    if used >= BUDGETS[key]: raise HTTPException(429, {"error": {"code": "budget_exceeded"}})
async def async_log_success_event(kwargs, response_obj, start, end):
    await redis.incrby(f"tokbudget:{key}:{today()}", response_obj.usage.total_tokens)  # expire at midnight+1h
```
Local aliases do not count against budgets (they cost nothing but CPU); only `*-cloud` aliases count. `eval-harness` has its own larger budget.

---

## 7. Key logic

### 7.1 Routing and fallback

```
caller -> alias
  pre-call hooks: metadata check -> pii_guard (cloud only) -> token_budget (cloud only)
  cache lookup (deterministic requests only)
  router.call(alias)
     on 429 / 5xx / timeout / safety-block:
         retry up to num_retries on the same deployment (respect retry_after)
         then follow fallbacks[alias]
  post-call: langfuse generation, prometheus, token_budget increment, headers
```
A fallback hop is recorded in the Langfuse generation (`metadata.fallback_from`). Crews are told nothing about the fallback except via the header; their evidence gates (design principle 5) do not depend on which model answered.

### 7.2 Structured output

| Alias family | Mechanism | Caller responsibility |
|---|---|---|
| Gemini | native `response_format: json_schema` | validate with Pydantic |
| Ollama | gateway translates `json_schema`/`json_object` into Ollama `format` (JSON schema accepted by recent Ollama; else `format: "json"`) | validate with Pydantic; on failure one repair retry with the validation error appended (implemented in crews) |

For CPU Ollama set `stream: false` for JSON calls, `temperature: 0`, and `num_predict` ≤ 1024 to bound latency (caller passes `max_tokens`; gateway enforces a ceiling of 2048 via config `max_tokens` on each deployment).

### 7.3 Caching rules

- Cache only when `temperature <= 0.1` and `metadata.no_cache` is not true and call is not a vision call.
- Key = hash(alias, messages, response_format, max_tokens, temperature). Prompt version is part of `messages` so editing a prompt invalidates automatically.
- TTL 24 h; eval runs set `metadata.cache_ttl=604800` (7 days) via the `eval-harness` key so a whole eval series reproduces.
- Production-like demo runs set `no_cache=true` on one sampled call per claim to guard against stale answers (PROPOSED, optional).
- Cache hits are still traced (tag `cache:hit`) with zero cost.

### 7.4 Offline / local mode (`LLM_MODE=local`)

Compose overlay mounts `config.local.yaml` as the config. It re-declares every `*-cloud` alias pointing to a local model:

| Alias | Local mapping |
|---|---|
| `reason-cloud` | `ollama/llama3.1:8b` |
| `cleanup-cloud` | `ollama/llama3.2:3b` |
| `vision-cloud` | removed; vision-service must degrade (`needs_human` for stamp escalation) |
| fallbacks | removed (nothing to fall back to) |

`make llm-local` restarts the gateway with the overlay. Evaluation reports must record `LLM_MODE` because quality differs.

### 7.5 Prompt registry

- Source of truth for production prompts: Langfuse Prompt Management (name `agent/<agent_name>`, label `production`, version integer).
- Each crew repo keeps `prompts/<agent>.<version>.md` as a **fallback** loaded when Langfuse is unreachable. A CI test asserts that file versions match the label in a Langfuse export committed at `prompts/langfuse_export.json` (updated by script).
- Callers send `metadata.prompt_version = "<agent>@<n>"`. The gateway does not fetch prompts; crews do. The gateway only requires the field so every generation can be joined to a prompt version.
- Every audit event for an agent output (`01-04` `model_info.prompt_version`) uses this same string.

### 7.6 Trace conventions (`TRACING.md`)

| Langfuse field | Value |
|---|---|
| `session_id` | `claim_ref` (hospital id) |
| `trace name` | `<system>.<stage>` e.g. `insurer.verification`, `hospital.intake` |
| trace `user_id` | pseudonymous role (`system:insurer-crew`), never a person's name |
| generation `name` | agent name |
| tags | `system:<x>`, `agent:<x>`, `stage:<x>`, `cache:hit|miss`, `fallback:<a>-><b>` |
| metadata | `prompt_version`, `config_versions`, `llm_mode` |

The crews create the **parent trace** (via the Langfuse SDK / OpenTelemetry) and pass `trace_id` in metadata; the gateway attaches its generation as a child (`langfuse_trace_id`, `langfuse_parent_observation_id` mapped from metadata). If no trace_id is present the gateway creates a root trace per call.

### 7.7 Ollama operations

- Resident model policy: with `MAX_LOADED_MODELS=1` and 16 GB, avoid alternating 3B/8B in tight loops. Policy: doc-pipeline uses `cleanup-local` (3B) in batch during intake; crews use cloud `reason-cloud` with fallback to 8B. When both are needed concurrently the gateway serialises; observed swap cost 20-60 s is budgeted in section 9 latency tests.
- GPU optional: compose has a commented `deploy.resources.reservations.devices` block.
- Model file overrides (optional): `Modelfile.llama3.2-3b-json` sets `PARAMETER temperature 0`, `PARAMETER num_ctx 8192`, `SYSTEM` empty; create with `ollama create llama3.2-3b-json -f ...` and point `cleanup-local` at it if JSON adherence is poor.

### 7.8 Langfuse operations

- Retention: Langfuse Postgres volume capped; nightly job exports the last 7 days to MinIO `kb-sources/langfuse-export/` (optional) and deletes traces older than 30 days using the public API (PROPOSED).
- Datasets: the eval harness (05-integration/02) writes runs into Langfuse datasets named `eval/<date>/<scenario_set>` so scores are visible next to traces.
- Scores: crews post `evidence_gate_passed` (0/1) and human-override events as scores on the trace.

---

## 8. Edge cases and failure handling

| # | Situation | Behaviour |
|---|---|---|
| 1 | Provider free-tier rate limit (≈10 rpm) | Router rpm queues briefly then falls back to local; `x-llm-fallback-used: true`; metric `llm_fallback_total` increments; eval report shows model mix. |
| 2 | Ollama cold start (20-60 s) | Warm-up job; gateway timeout for local aliases 120-180 s; first call after idle may be slow. Callers set generous timeouts for `*-local`. |
| 3 | Two heavy models requested concurrently on 16 GB | `MAX_LOADED_MODELS=1`, `NUM_PARALLEL=1`; requests serialise at the gateway (`max_parallel_requests`); callers see higher latency but no OOM. |
| 4 | Gemini safety block on medical text | Treated as provider failure; falls back to local; Langfuse tag `safety_block`. If local also fails → 502 `provider_error` → crew sets `needs_human`. |
| 5 | Langfuse down | Callbacks are async and fail-open; requests unaffected; gateway buffers up to 1000 events in memory then drops oldest, logging a warning. |
| 6 | Redis down | Cache disabled (bypass), token_budget fail-open **but** logs critical; PII guard still enforced (stateless). |
| 7 | Gateway DB down | `allow_requests_on_db_unavailable: true` lets cached keys keep working; key admin endpoints fail. |
| 8 | PII guard false positive (12-digit invoice number) | Currency-prefix allowance; otherwise caller must reformat (e.g. insert separators like `INV-`). Documented in crews' prompt-construction helper. Optionally `PII_GUARD_MODE=log` in dev. |
| 9 | PII guard false negative | Presidio in doc-pipeline is the primary control; guard is a net. Weekly eval checks a PII canary corpus (05-integration/03). |
| 10 | Embedding model swapped | `x-embed-model` header changes; rag-service refuses to write/search mismatched collections and requires `reindex` (doc 05). |
| 11 | Dimension mismatch from fallback | Disabled by config; if triggered the embed call returns 502 rather than a wrong-size vector. |
| 12 | Oversized prompt | `context_window_fallbacks` escalate local 3B → 8B; if still too large gateway returns 400 `context_too_long`; crews chunk. |
| 13 | Malformed JSON from local model | Not gateway's concern; crews repair-retry once then `needs_human`. |
| 14 | Key leaked in logs | Gateway logs redact `Authorization`; `json_logs` + a log filter strips any `sk-` pattern. CI test greps container logs. |
| 15 | Budget exhaustion mid-claim | 429 `budget_exceeded`; crew marks the step `needs_human` with reason `llm_budget`; the claim continues manually (design principle 7: fixable before fatal). |
| 16 | Clock drift/Redis TTL for budgets | Budget keys use UTC date; midnight rollover tested. |
| 17 | Streaming requests | Not used in v1. Gateway rejects `stream: true` for JSON calls with 400 to avoid partial JSON handling. |
| 18 | Vision call with base64 images | PII guard skips binary content but checks any accompanying text; cropped regions only (vision-service contract). |

---

## 9. Tests

### 9.1 Unit/integration matrix

| ID | Test | Expected |
|---|---|---|
| T1 | Alias resolution for all aliases (fake provider via `mock_response`) | each returns |
| T2 | Fallback: mock `reason-cloud` returns 429 | response served by `reason-local`; header `x-llm-fallback-used: true` |
| T3 | Fallback chain exhausted | 502 `provider_error` |
| T4 | `pii_guard` Aadhaar `2345 6789 0123` to `reason-cloud` | 400 `pii_detected`, log has no digits |
| T5 | `pii_guard` PAN, mobile, email, raw member id | all 400 |
| T6 | Same text to `reason-local` | allowed |
| T7 | Presidio placeholders `<PERSON_1>` to cloud | allowed |
| T8 | 12-digit amount `INR 123456789012` to cloud | allowed (currency allowance) |
| T9 | Cache hit identical deterministic request | second call tagged `cache:hit`, latency < 50 ms |
| T10 | `no_cache=true` / `temperature=0.7` | cache bypassed |
| T11 | Virtual key restricted to its aliases | 403 on others |
| T12 | Token budget | after limit 429 `budget_exceeded`; resets next UTC day (clock mocked) |
| T13 | Missing metadata | 400 `metadata_required` |
| T14 | Embedding returns 768 dims and `x-embed-model` header | pass |
| T15 | Langfuse generation exists with `session_id=claim_ref`, tags, `prompt_version` | API check |
| T16 | Langfuse down | request still succeeds, p95 unaffected |
| T17 | `LLM_MODE=local` | `reason-cloud` served by Ollama; no outbound traffic (iptables/egress-block check in compose test) |
| T18 | Rotation script | old key rejected, new accepted |
| T19 | CI grep for provider keys | passes; fails if key added to another service env |
| T20 | Log redaction | no `Authorization` or `sk-` strings in container logs after test run |

### 9.2 Performance/load

- 20 concurrent `cleanup-local` requests (200-token prompts): no gateway crash; p95 < 40 s; no OOM (monitor `docker stats`).
- 10 concurrent `reason-cloud` with rpm=10: all succeed (some via fallback); fallback ratio recorded.
- Cold-start test: restart Ollama, send request without warm-up, record latency (informational; threshold 90 s).
- Soak: 1 hour of 1 request/5 s mixed aliases; memory of gateway stable (< 1 GB).

### 9.3 Observability checks
`scripts/smoke.sh` prints a table: alias, status, latency, served-by model, trace url. Used in CI nightly (optional) and by Dev A to verify the environment before starting.

---

## 10. Acceptance criteria

- [ ] All six aliases answer a hello prompt via `smoke.sh`.
- [ ] Removing `GEMINI_API_KEY` still yields answers from `reason-cloud` via fallback.
- [ ] No provider key present in any other service's env (CI grep green).
- [ ] PII guard test suite green; no raw match text in logs.
- [ ] Every call visible in Langfuse with `claim_ref` as session, `agent`, `prompt_version`.
- [ ] `cost_by_claim.py` produces per-claim token/latency totals for a synthetic run.
- [ ] `LLM_MODE=local` works with the internet disabled.
- [ ] Virtual keys, rpm limits and token budgets enforced as in section 2.3.
- [ ] Gateway + Ollama + Langfuse fit the compose memory budget (≤ 8.5 GB combined with a 3B model resident) documented in `infra/compose/README.md`.

---

## 11. Dependencies

| Depends on | For |
|---|---|
| Redis (04-01) | cache, router state, token budgets, SSE not used here |
| Postgres (two small DBs) | LiteLLM keys/spend, Langfuse |
| `01-shared-contract/05` | env, compose profile `llm` |
| doc `05-integration-and-eval/03` | PII patterns/canary corpus |

Consumed by: doc-pipeline (04-02), vision-service (04-03), rag-service (04-05), both crews, both n8n, tpa-sim (optional), eval harness.

Interface freeze: aliases, metadata fields and error codes in sections 3, 5.2 and 5.3 are the contract other docs rely on. Changes need both developers' approval.

---

## 12. Claude Code kickoff prompt

> Implement docs/implementation/04-shared-services/04-llm-gateway-ollama-langfuse.md tasks 1-13 in order. Verify with `scripts/smoke.sh` after tasks 2, 4 and 6, and run the test matrix in section 9 before finishing. Never commit real API keys; use `.env.example` and the gitignored `.keys.env`. Pin Langfuse v2 unless I tell you otherwise. Report any deviation from the alias table in section 3.
