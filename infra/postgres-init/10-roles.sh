#!/bin/bash
# First-start bootstrap for hospital-db (01-hospital-db §3.5, task 2). Roles are cluster-level;
# schema objects are created later by Alembic running as hosp_owner.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE hosp_owner LOGIN CREATEDB PASSWORD '${HOSP_OWNER_PW}';
CREATE ROLE hosp_app LOGIN PASSWORD '${HOSP_APP_PW}';
CREATE ROLE hosp_readonly LOGIN PASSWORD '${HOSP_READONLY_PW}';
CREATE ROLE n8n_app LOGIN PASSWORD '${N8N_APP_PW}';
ALTER DATABASE ${POSTGRES_DB} OWNER TO hosp_owner;
REVOKE CONNECT ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO hosp_owner, hosp_app, hosp_readonly;
CREATE DATABASE n8n OWNER n8n_app;
REVOKE CONNECT ON DATABASE n8n FROM PUBLIC;
SQL
