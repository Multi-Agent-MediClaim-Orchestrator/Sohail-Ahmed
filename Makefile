COMPOSE = docker compose --env-file .env -f infra/compose/docker-compose.base.yml -f infra/compose/shared.yml
.PHONY: fixtures schemas migrate seed db-reset db-shell db-dump db-restore test-db test-infra render kc-reset init init-secrets up-infra down nuke smoke test lint fmt typecheck config-check

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
	uv run pytest infra/tests -q -m integration

test:
	uv run pytest contract/tests hospital/api/tests infra/tests -q

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
	uv run pytest hospital/api/tests -q
