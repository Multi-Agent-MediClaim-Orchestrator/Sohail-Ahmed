#!/usr/bin/env bash
# Pull the local models (about 9 GB on first run). Skips models already present.
set -euo pipefail
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
run() { if command -v ollama >/dev/null 2>&1 && [ -z "${USE_DOCKER:-}" ]; then ollama "$@"; else docker exec "$OLLAMA_CONTAINER" ollama "$@"; fi; }
have="$(run list | awk 'NR>1 {print $1}')"
for m in llama3.2:3b llama3.1:8b nomic-embed-text; do
  if echo "$have" | grep -q "^${m}\(:latest\)\?$"; then echo "present: $m"; else echo "pulling: $m"; run pull "$m"; fi
done
run list
