#!/usr/bin/env bash
# Browser tests of the UI against the real stack (rules mode, no model): starts the stack and the UI, runs Playwright in the
# system Chrome, stops everything. Needs `make up-infra` and `make up-n8n`.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
rm -rf /tmp/ui-e2e-corpus
uv run python -m synth.corpus --out /tmp/ui-e2e-corpus -n 1 --seed 42 --archetype S01 >/dev/null
E2E_CASE_DIR=/tmp/ui-e2e-corpus/SYN-000000
(cd hospital/ui && npm run build >/tmp/ui-build.log 2>&1) || { echo "UI build failed"; tail -20 /tmp/ui-build.log; exit 2; }
uv run python scripts/e2e_hospital.py --serve > /tmp/ui-serve.log 2>&1 &
SERVE=$!
for i in $(seq 1 180); do grep -q READY /tmp/ui-serve.log && break; sleep 1; done
grep -q READY /tmp/ui-serve.log || { echo "stack did not start"; tail -20 /tmp/ui-serve.log; kill $SERVE; exit 2; }
(cd hospital/ui && E2E_CASE_DIR="$E2E_CASE_DIR" DEMO_PW="$DEMO_PW" npx playwright test "$@")
rc=$?
kill -TERM $SERVE; wait $SERVE 2>/dev/null
exit $rc
