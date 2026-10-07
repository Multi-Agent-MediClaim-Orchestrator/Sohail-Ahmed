LOCK = scripts/run_exclusive.sh
# CrewAI: no telemetry leaves the machine, for every recipe (an existing .env may predate these keys)
export CREWAI_TELEMETRY_OPT_OUT = true
export OTEL_SDK_DISABLED = true
COMPOSE = docker compose --env-file .env -f infra/compose/docker-compose.base.yml -f infra/compose/shared.yml -f infra/compose/hospital.yml
.PHONY: crew-demo ins-crew-demo crew-plot up-rag seed-kb run-rag rag-tokens ins-ui-e2e check-crew up-insurer-n8n n8n-insurer-test db-reset-insurer e2e-full up-insurer migrate-insurer seed-insurer run-insurer-api run-calc run-insurer-crew run-tpa-sim test-insurer train-stamps eval-stamps ui-e2e e2e-hospital seed-data seed-golden eval-hospital run-vision test-llm run-docpipe run-ui run-api run-crew flows up-n8n n8n-test fixtures schemas migrate seed db-reset db-shell db-dump db-restore test-db test-infra render kc-reset init init-secrets up-infra down nuke smoke test lint fmt typecheck config-check

init: init-secrets
	uv sync --all-packages
	-pre-commit install

init-secrets:
	@test -f .env || cp .env.example .env
	@python3 scripts/gen_secrets.py

render:
	python3 scripts/render_infra.py && python3 infra/keycloak/render_realms.py

up-infra: render
	$(COMPOSE) --profile infra up -d --wait
	set -a && . ./.env && set +a && MINIO_ENDPOINT=localhost:$${SHARED_MINIO_PORT:-9000} uv run python infra/minio/init.py

flows:
	uv run python hospital/n8n/build_flows.py && uv run python scripts/lint_flows.py

up-n8n:
	$(COMPOSE) --profile hospital up -d --wait hospital-n8n

n8n-test:
	$(LOCK) uv run pytest hospital/n8n -q -m n8n

run-api:
	cd hospital/api && uv run uvicorn app.asgi:app --port 8100

run-ui:
	cd hospital/ui && npm install --no-audit --no-fund && HOSP_API_URL=http://localhost:8100 npx next dev -p 3100

run-crew:
	set -a && . ./.env && set +a && uv run uvicorn crew.main:app_factory --factory --app-dir hospital/crew --host 127.0.0.1 --port 8010

crew-demo: ## hospital CrewAI flows on built-in synthetic data (CREW_LLM=ollama for the real model)
	cd hospital/crew && uv run crewai run

ins-crew-demo: ## insurer CrewAI flows on built-in synthetic claims (INS_CREW_LLM=ollama for the real model)
	cd insurer/crew && uv run crewai run

crew-plot: ## hospital and insurer flow diagrams (HTML) into docs/diagrams
	mkdir -p docs/diagrams && cd docs/diagrams && uv run --package hospital-crew plot && uv run --package insurer-crew plot

test-llm:
	$(LOCK) uv run pytest hospital/crew services/doc-pipeline -q -m llm -s

run-vision:
	set -a && . ./.env && set +a && cd services/vision-service && uv run uvicorn vision.main:app_factory --factory --host 127.0.0.1 --port 8300

run-docpipe:
	set -a && . ./.env && set +a && cd services/doc-pipeline && uv run uvicorn docpipe.main:app_factory --factory --host 127.0.0.1 --port 8200

seed-data:
	uv run python -m synth.corpus --out data/synthetic/out -n $${N:-100} --seed $${SEED:-42}

seed-golden:
	uv run python -m synth.corpus --out data/synthetic/out -n $${N:-100} --seed $${SEED:-42} --golden

eval-hospital:
	uv run python -m evalh.run --corpus data/synthetic/out --out data/eval/report.json

e2e-hospital:
	$(LOCK) uv run python scripts/e2e_hospital.py

e2e-full:
	$(LOCK) uv run python scripts/e2e_full.py

ui-e2e:
	$(LOCK) scripts/ui_e2e.sh

train-stamps:
	uv run python -m evalh.train_stamps --n 450

eval-stamps:
	uv run python -m evalh.stamps_eval --n 90 --seed 99

down:
	$(COMPOSE) --profile infra down

nuke:
	@read -p "Delete ALL volumes? [y/N] " a && [ "$$a" = y ] && $(COMPOSE) --profile infra down -v

config-check:
	$(COMPOSE) --profile infra config -q

smoke:
	bash scripts/smoke.sh

kc-reset:
	$(COMPOSE) --profile infra up -d --force-recreate keycloak

test-infra:
	$(LOCK) uv run pytest infra/tests -q -m integration

test:
	$(LOCK) uv run pytest contract/tests hospital/api/tests hospital/crew infra/tests -q
	$(LOCK) uv run pytest services/doc-pipeline -q
	$(LOCK) uv run pytest services/vision-service -q
	$(LOCK) uv run pytest data/synthetic data/eval -q

lint:
	uv run ruff check .
fmt:
	uv run ruff format .
typecheck:
	uv run mypy contract/python/claim_contract

fixtures:
	uv run python scripts/gen_fixtures.py

schemas:
	uv run python scripts/export_schemas.py

migrate:
	cd hospital/api && uv run alembic upgrade head

seed:
	cd hospital/api && uv run python -m seed.run

db-reset:
	$(COMPOSE) --profile infra rm -sf hospital-db
	-docker volume rm claims_hospital_pg
	$(COMPOSE) --profile infra up -d --wait hospital-db
	$(MAKE) migrate seed

db-shell:
	set -a && . ./.env && set +a && PGPASSWORD=$$HOSP_APP_PW psql -h localhost -p $${HOSP_DB_PORT:-5432} -U hosp_app -d hospital

db-dump:
	@mkdir -p backups
	set -a && . ./.env && set +a && docker exec -e PGPASSWORD=$$HOSP_OWNER_PW claims-hospital-db-1 pg_dump -U hosp_owner -Fc hospital > backups/hospital-$$(date +%Y%m%d-%H%M%S).dump

db-restore:
	@test -n "$(FILE)" || (echo "usage: make db-restore FILE=backups/x.dump"; exit 1)
	set -a && . ./.env && set +a && docker exec -i -e PGPASSWORD=$$HOSP_OWNER_PW claims-hospital-db-1 pg_restore -U hosp_owner -d hospital --clean --if-exists --no-owner < $(FILE)

test-db:
	$(LOCK) uv run pytest hospital/api/tests -q

# ---- insurer side (Dev B code, host-run like the hospital side) ----
INS_ENV = set -a && . ./.env && set +a && \
	export INS_APP_PASSWORD INS_RO_PASSWORD INS_DATABASE_URL=postgresql+asyncpg://ins_app:$$INS_APP_PASSWORD@localhost:$${INS_DB_PORT:-5453}/insurer \
	INS_OWNER_DATABASE_URL=postgresql://postgres:$$INS_PG_SUPERUSER_PASSWORD@localhost:$${INS_DB_PORT:-5453}/insurer \
	INS_REDIS_URL=redis://ins_app:$$INS_REDIS_PW@localhost:$${SHARED_REDIS_PORT:-6379}/0 \
	INS_EVENTS_STREAM=sse:insurer:events \
	INS_HMAC_SECRETS='{"hosp-001": ["'$$HOSP_TO_INS_HMAC_SECRET'"], "bank-sim": ["dev-bank-sim-callback-secret-00000000000"]}' \
	INS_INS_TO_HOSP_HMAC_SECRET=$$INS_TO_HOSP_HMAC_SECRET \
	INS_HOSPITAL_CALLBACK_BASE=http://localhost:8100 \
	INS_N8N_URL=http://localhost:5689

up-insurer: render
	$(COMPOSE) --profile insurer up -d --wait insurer-db

migrate-insurer:
	$(INS_ENV) && cd insurer/api && uv run alembic upgrade head

seed-insurer:
	$(INS_ENV) && INS_SEED_PROFILE=demo uv run python -m insurer_app.seeds.seed

run-insurer-api:
	$(INS_ENV) && cd insurer/api && uv run uvicorn insurer_app.main:create_app --factory --port $${INS_API_PORT:-8600}

run-calc:
	cd insurer/calc_engine && uv run uvicorn calc_engine.api:app --port $${INS_CALC_PORT:-8620}

run-insurer-crew:
	cd insurer/crew && uv run uvicorn insurer_crew.main:app --port $${INS_CREW_PORT:-8610}

run-tpa-sim: 
	cd services/tpa-sim && uv run uvicorn tpa_sim.main:app --port 8500

test-insurer:
	$(LOCK) uv run pytest insurer/calc_engine insurer/crew insurer/n8n services/tpa-sim services/rag-service infra/llm-gateway -q
	$(LOCK) uv run pytest insurer/api -q

db-reset-insurer:
	$(COMPOSE) --profile insurer rm -sfv insurer-db
	-docker volume rm claims_insurer_pg

up-insurer-n8n: init-secrets
	$(COMPOSE) -f infra/compose/insurer.yml --profile n8n up -d --force-recreate --wait insurer-n8n

check-crew: ## known-answer check of the insurer crew on the real local model (needs Ollama)
	uv run python scripts/check_insurer_crew.py

ins-ui-e2e: ## insurer UI browser tests against the real stack (system Chrome)
	$(LOCK) scripts/ins_ui_e2e.sh

# ---- RAG (Qdrant + local Ollama; no gateway: Ollama's OpenAI-compatible endpoint serves embeddings and answers) ----
RAG_ENV = set -a && . ./.env && set +a && \
	export STORE=qdrant EMBEDDER=gateway QDRANT_URL=http://localhost:6333 QDRANT_API_KEY=$$QDRANT_API_KEY \
	LLM_GATEWAY_URL=http://localhost:11434 LLM_GATEWAY_KEY=ollama EMBED_ALIAS=$${RAG_EMBED_MODEL:-nomic-embed-text:latest} \
	CHAT_ALIAS=$${RAG_CHAT_MODEL:-gemma4:latest} CHAT_REASONING_EFFORT=none DOCPIPE_URL= DB_URL="$(CURDIR)/.e2e-logs/rag.db" JWT_SECRET=$$RAG_JWT_SECRET RAG_PORT=$${RAG_PORT:-8400}

up-rag: init-secrets
	docker compose --env-file .env -f infra/compose/docker-compose.base.yml -f infra/compose/ai.yml --profile rag up -d --wait qdrant

seed-kb: ## build the synthetic knowledge base in Qdrant with real embeddings
	$(RAG_ENV) && uv run python services/rag-service/scripts/seed_kb.py

run-rag:
	$(RAG_ENV) && cd services/rag-service && uv run uvicorn rag_service.main:app --port $${RAG_PORT:-8400}

rag-tokens: ## service tokens (JSON) for the crews and the API
	$(RAG_ENV) && uv run python services/rag-service/scripts/issue_service_tokens.py
