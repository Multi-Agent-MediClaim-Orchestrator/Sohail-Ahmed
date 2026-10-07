#!/usr/bin/env bash
# Load each local model once so the first real request is fast (04-04 task 9).
set -euo pipefail
GW="${GW:-http://localhost:4000}"
KEY="${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY}"
meta='"metadata":{"system":"shared","agent":"warmup","prompt_version":"warmup@1","no_cache":true}'
for alias in cleanup-local reason-local; do
  curl -fsS -m 240 "$GW/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$alias\",\"max_tokens\":5,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],$meta}" >/dev/null && echo "warm: $alias"
done
curl -fsS -m 120 "$GW/v1/embeddings" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"embed\",\"input\":[\"warm up\"],$meta}" >/dev/null && echo "warm: embed"
