#!/usr/bin/env sh
# Import the generated flows into n8n and activate only the trigger flows listed in active_flows.txt (sub-workflows cannot be activated).
# Usage inside the n8n container:  sh /scripts/n8n_import.sh /n8n/flows
set -eu
DIR="${1:-/flows}"
n8n import:workflow --separate --input="$DIR"
for id in $(tr -d '\r' < "$DIR/../active_flows.txt"); do
  n8n update:workflow --id="$id" --active=true < /dev/null
done
