#!/bin/sh
# 1. create the Keycloak client-credentials credential from env (secrets never live in the repo or the flow files)
# 2. import the committed flows (stable ids: re-import updates in place), 3. activate trigger flows + the error handler, 4. run n8n.
set -e
cat > /tmp/credentials.json <<EOF
[{"id":"insurer-keycloak-client","name":"insurer-keycloak-client","type":"oAuth2Api","data":{
  "grantType":"clientCredentials","accessTokenUrl":"$INS_KEYCLOAK_TOKEN_URL","clientId":"$INS_N8N_CLIENT_ID",
  "clientSecret":"$INS_N8N_CLIENT_SECRET","authentication":"body","scope":"","authUrl":"","authQueryParameters":"",
  "ignoreSSLIssues":false}}]
EOF
n8n import:credentials --input=/tmp/credentials.json
rm -f /tmp/credentials.json
n8n import:workflow --separate --input=/flows
for id in $(cat /active_flows.txt); do
  n8n publish:workflow --id="$id" >/dev/null
done
exec n8n start
