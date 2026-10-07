#!/usr/bin/env bash
# Every alias answers a hello prompt; prints alias, status, latency, served-by model.
set -uo pipefail
GW="${GW:-http://localhost:4000}"
KEY="${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY}"
meta='"metadata":{"system":"shared","agent":"smoke","prompt_version":"smoke@1","no_cache":true,"session_id":"smoke"}'
printf '%-16s %-6s %-8s %s\n' alias status secs served_by
fail=0
for alias in cleanup-local cleanup-cloud reason-local reason-cloud vision-cloud; do
  out=$(curl -s -o /tmp/smoke.$$ -w '%{http_code} %{time_total}' -m 240 "$GW/v1/chat/completions" -H "Authorization: Bearer $KEY" \
        -H 'Content-Type: application/json' -d "{\"model\":\"$alias\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}],$meta}")
  code=${out% *}; secs=${out#* }
  served=$(sed -n 's/.*"model":"\([^"]*\)".*/\1/p' /tmp/smoke.$$ | head -1)
  printf '%-16s %-6s %-8s %s\n' "$alias" "$code" "$secs" "${served:-?}"
  [ "$code" = "200" ] || fail=1
done
code=$(curl -s -o /tmp/smoke.$$ -w '%{http_code}' -m 120 "$GW/v1/embeddings" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "{\"model\":\"embed\",\"input\":[\"hello\"],$meta}")
printf '%-16s %-6s\n' embed "$code"
[ "$code" = "200" ] || fail=1
rm -f /tmp/smoke.$$
exit $fail
