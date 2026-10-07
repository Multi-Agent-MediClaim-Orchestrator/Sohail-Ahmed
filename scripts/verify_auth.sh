#!/usr/bin/env bash
# Smoke-check authentication and role gates against a running hospital-api (default :8100).
set -uo pipefail
cd "$(dirname "$0")/.."
API="${HOSP_API_URL:-http://localhost:8100}"
fail=0
check() { # name expected_status method path user
  local code
  if [ "$5" = "none" ]; then code=$(curl -s -o /dev/null -w '%{http_code}' -X "$3" "$API$4")
  else code=$(curl -s -o /dev/null -w '%{http_code}' -X "$3" -H "Authorization: Bearer $(scripts/get_token.sh "$5")" "$API$4"); fi
  if [ "$code" = "$2" ]; then echo "ok   $1 ($code)"; else echo "FAIL $1: expected $2 got $code"; fail=1; fi
}
check "no token is rejected"            401 GET /v1/me none
check "desk can read own profile"       200 GET /v1/me desk1
check "desk cannot list users"          403 GET /v1/admin/users desk1
check "officer cannot list users"       403 GET /v1/admin/users officer1
check "admin can list users"            200 GET /v1/admin/users hadmin
check "admin cannot read cases"         403 GET /v1/dashboard/summary hadmin
check "officer reads the dashboard"     200 GET /v1/dashboard/summary officer1
check "internal jobs reject humans"     403 POST /v1/internal/jobs/sweeper officer1
exit $fail
