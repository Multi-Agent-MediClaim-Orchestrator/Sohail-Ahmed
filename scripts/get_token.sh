#!/usr/bin/env bash
# Print an access token for a demo user from the hospital realm (dev client, password grant).
#   scripts/get_token.sh officer1            -> token on stdout
#   curl -H "Authorization: Bearer $(scripts/get_token.sh officer1)" localhost:8100/v1/me
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
user="${1:?usage: get_token.sh <desk1|officer1|officer2|hadmin>}"
curl -sf -X POST "${KEYCLOAK_TOKEN_URL:-http://localhost:8080/realms/hospital/protocol/openid-connect/token}" \
  -d grant_type=password -d client_id=hospital-dev -d "username=${user}" -d "password=${DEMO_PW}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
