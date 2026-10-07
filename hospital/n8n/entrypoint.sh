#!/bin/sh
# Import the committed flows (stable ids: re-import updates in place), activate them, then run n8n.
set -e
n8n import:workflow --separate --input=/flows
for f in /flows/*.json; do
  id=$(basename "$f" .json)
  n8n publish:workflow --id="$id" >/dev/null
done
exec n8n start
