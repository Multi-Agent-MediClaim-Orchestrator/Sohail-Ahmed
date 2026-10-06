#!/usr/bin/env bash
set -u
[ -f .env ] && set -a && . ./.env && set +a
fail=0
chk() { if curl -fsS -m 5 "$2" >/dev/null 2>&1; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
chk minio http://localhost:${SHARED_MINIO_PORT:-9000}/minio/health/live
chk keycloak http://localhost:9090/health/ready
docker compose ps --format '{{.Service}} {{.Health}}' | grep -E 'hospital-db|redis|clamav' | grep -v healthy && fail=1
exit $fail
