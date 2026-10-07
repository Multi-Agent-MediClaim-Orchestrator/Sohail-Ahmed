#!/usr/bin/env bash
# Browser tests of the insurer UI against the real hospital + insurer stack (rules mode): seeds claims in distinct states,
# starts the UI, runs Playwright in the system Chrome, stops everything. Needs `make up-infra up-n8n up-insurer`.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
(cd insurer/ui && npm run build >/tmp/ins-ui-build.log 2>&1) || { echo "insurer UI build failed"; tail -20 /tmp/ins-ui-build.log; exit 2; }
uv run python scripts/e2e_full.py --serve > /tmp/ins-ui-serve.log 2>&1 &
SERVE=$!
for i in $(seq 1 420); do grep -q READY /tmp/ins-ui-serve.log && break; sleep 1; done
grep -q READY /tmp/ins-ui-serve.log || { echo "stack did not start"; tail -30 /tmp/ins-ui-serve.log; kill $SERVE; exit 2; }
(cd insurer/ui && INS_DEV_JWT_SECRET="${INS_DEV_JWT_SECRET:-dev-insurer-api-jwt-secret-0000000000000000}" npx playwright test "$@")
rc=$?
kill -TERM $SERVE; wait $SERVE 2>/dev/null
exit $rc
