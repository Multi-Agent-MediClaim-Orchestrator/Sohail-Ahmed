#!/usr/bin/env bash
# Run a command while holding the repo-wide run lock, so the test suites and the end-to-end runs queue instead of
# overlapping (one run failed 88 tests when a suite shared the machine with the real-model e2e).
#   scripts/run_exclusive.sh uv run pytest ...
set -euo pipefail
cd "$(dirname "$0")/.."
exec 9>.run.lock
if ! flock -n 9; then
  echo "another test or e2e run holds .run.lock; waiting for it..." >&2
  flock 9
fi
"$@"
