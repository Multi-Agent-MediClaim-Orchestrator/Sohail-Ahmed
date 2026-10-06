# 04-01 — Infra: Redis, MinIO, ClamAV, Keycloak

Owner: **Dev A**. Status: PROPOSED where marked. Conventions: see `01-shared-contract/05-repo-layout-and-conventions.md`.

## 0. Table of contents
1. Goal · 2. Inputs/Outputs · 3. Data model (Redis, MinIO, Keycloak) · 4. API/endpoints · 5. Build tasks (with full configs) · 6. Key logic/pseudocode · 7. Config/env vars · 8. Error handling and edge cases · 9. Tests · 10. Acceptance criteria · 11. Dependencies · 12. Claude Code kickoff prompt

## 1. Goal
Provide four infrastructure services that every other component depends on:

| Service | Role | Why it is Dev A's |
|---|---|---|
| Redis 7 | queue (n8n), cache (LiteLLM), pub/sub (SSE, config, audit), idempotency, rate limits, locks | hospital-api needs it first |
| MinIO | encrypted object storage, three buckets | hospital upload path is first consumer |
| ClamAV | virus scan of every upload | hospital upload path |
| Keycloak | two realms (`hospital`, `insurer`), OIDC | both UIs and APIs need tokens in Phase 1 |

Requirements:
1. Start healthy from `make up-infra` on a 16 GB machine within a 4 GB combined budget for these four services.
2. Fully reproducible from files in git (no manual console clicking). Realms, buckets, policies, ACLs are all code.
3. Strict isolation between the two systems: separate Redis ACL users and key prefixes, separate MinIO service users, separate Keycloak realms, token issuer pinned per API.
4. Fail closed for security checks (virus scan, JWT validation), fail open only for rate limiting.
5. Provide copy-able client helper patterns that Dev B reuses without a shared runtime library (keeps the systems independent).

Non-goals: production HA, TLS termination on every internal hop (dev uses plain HTTP inside the compose network; `mkcert` TLS only at the edge for the inter-system API per contract 01-01), real KMS.

## 2. Inputs / Outputs

| Service | Consumers | Produces |
|---|---|---|
| Redis 7 | n8n (queue mode, both systems), LiteLLM cache, SSE pub/sub, idempotency keys (01-01 §4), config invalidation (`cfg:changed`), audit fan-out (`audit.appended`), doc-pipeline job stream | Key-value, streams, pub/sub |
| MinIO | hospital-api, insurer-api, doc-pipeline (read-only), vision-service (read-only), rag-service (kb-sources read-only) | Buckets `hospital-docs`, `insurer-docs`, `kb-sources`; presigned URLs |
| ClamAV | hospital-api (upload), insurer-api (document copy-in from presigned URL) | Verdicts `OK` / `<sig> FOUND` / `ERROR` |
| Keycloak | both UIs (OIDC auth-code + PKCE), both APIs (JWT validation), n8n and crews (client-credentials service accounts) | Realms, JWKS, access tokens with roles |

## 3. Data model

### 3.1 Redis

#### 3.1.1 Database indexes
| DB | Use | Owner user |
|---|---|---|
| 0 | n8n hospital queue (Bull) | `n8n_hosp` |
| 1 | n8n insurer queue (Bull) | `n8n_ins` |
| 2 | LiteLLM cache and rate state | `llm` |
| 3 | app idempotency, rate limit, locks (both systems, prefix separated) | `hosp_app`, `ins_app` |
| 4 | pub/sub channels and streams | `hosp_app`, `ins_app`, `docpipe` |

Note: Redis ACL key patterns apply across DB indexes (ACL is not per DB); isolation is by key prefix and channel pattern, DB index is organisational only.

#### 3.1.2 Key namespaces
| Pattern | Type | Purpose | TTL |
|---|---|---|---|
| `idem:hosp:{key_id}:{idem_key}` | hash `{req_hash,status,body,created}` | idempotency (hospital side) | 24 h |
| `idem:ins:{key_id}:{idem_key}` | hash | idempotency (insurer side) | 24 h |
| `cfg:changed` | pub/sub channel | message `{system,domain,version}` | n/a |
| `sse:hospital:{case_id}` | pub/sub channel | status feed fan-out | n/a |
| `sse:insurer:{case_id}` | pub/sub channel | status feed fan-out | n/a |
| `audit:appended` | pub/sub channel | `{system,case_id,seq}` | n/a |
| `llm:cache:*` | string | LiteLLM response cache | 24 h |
| `rl:{hosp|ins}:{key_id}:{minute}` | counter | rate limit | 120 s |
| `lock:{hosp|ins}:{resource}` | string | SET NX PX 30000 | 30 s |
| `docpipe:jobs` | stream, group `workers` | doc-pipeline jobs | trimmed to 10 000 |
| `docpipe:result:{job_id}` | string JSON | job result | 1 h |
| `vision:esc:{case_id}` | counter | vision escalation budget | 7 d |
| `bull:*`, `n8n:*` | n8n internal | queue | managed |

Persistence: AOF `everysec` plus RDB snapshot; this is a dev store, losing 1 s of data is acceptable. `maxmemory 200mb`, policy `volatile-lru` (never evict streams or keys without TTL).

### 3.2 MinIO

#### 3.2.1 Layout
```
hospital-docs/{case_id}/{doc_id}/original.{ext}
hospital-docs/{case_id}/{doc_id}/parsed.json
hospital-docs/{case_id}/{doc_id}/pii_map.enc
hospital-docs/{case_id}/{doc_id}/pages/{n}.png
hospital-docs/{case_id}/exports/claim-{claim_ref}.pdf
insurer-docs/{insurer_claim_no}/{doc_id}/original.{ext}
insurer-docs/{insurer_claim_no}/{doc_id}/parsed.json
insurer-docs/{insurer_claim_no}/{doc_id}/pages/{n}.png
kb-sources/{collection}/{filename}
```
Object user-metadata: `x-amz-meta-sha256`, `x-amz-meta-origin-system`. Object tags: `scan=clean|infected|pending`, `doc_type`.

#### 3.2.2 Bucket properties
| Bucket | Versioning | SSE | Lifecycle | Object lock |
|---|---|---|---|---|
| hospital-docs | on | SSE-S3 (KMS key `local-key`) | noncurrent expire 30 d; abort incomplete multipart 2 d | off |
| insurer-docs | on | SSE-S3 | same | off |
| kb-sources | on | SSE-S3 | none | off |
| audit-anchors (PROPOSED, 4th bucket created here, written by 01-04 nightly job) | on | SSE-S3 | none | COMPLIANCE, 365 d |

#### 3.2.3 IAM policies
`infra/minio/policies/hosp-rw.json`:
```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:GetObjectTagging","s3:PutObjectTagging","s3:GetObjectVersion"],"Resource":["arn:aws:s3:::hospital-docs/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::hospital-docs"]}
]}
```
`infra/minio/policies/ins-rw.json`: same shape on `insurer-docs` plus read on `kb-sources`.
`infra/minio/policies/docpipe-ro-hosp.json`: `s3:GetObject` and `s3:PutObject` ONLY on `hospital-docs/*/*/parsed.json`, `hospital-docs/*/*/pii_map.enc` and `hospital-docs/*/*/pages/*` (write limited to derived artefacts), read on `original.*`.
`infra/minio/policies/docpipe-ro-ins.json`: equivalent for `insurer-docs`.
`infra/minio/policies/vision-ro.json`: `GetObject` on both pages prefixes.
`infra/minio/policies/rag-ro.json`: `GetObject`, `ListBucket` on `kb-sources`.
`infra/minio/policies/anchor-w.json`: `PutObject` on `audit-anchors` only.

Service users: `hosp_svc`, `ins_svc`, `docpipe_hosp`, `docpipe_ins`, `vision_svc`, `rag_svc`, `anchor_svc`. Principle: `ins_svc` has no statement touching `hospital-docs` (verified by test 9.4); cross-system file exchange is ONLY via presigned URLs generated by the hospital (01-01 §6).

#### 3.2.4 Presigned URL rules
Method GET only, TTL 24 h default (max 72 h), generated by `hosp_svc` with `response-content-disposition=attachment`. URLs are never logged in full (log host + bucket + first 8 chars of key hash).

### 3.3 Keycloak

#### 3.3.1 Realms
Two independent realms in one Keycloak instance: `hospital` and `insurer`. Different signing keys (each realm has its own RS256 key), different user stores, different clients. A token from one realm is rejected by the other system's API because `iss` differs.

#### 3.3.2 Roles
| Realm | Realm roles |
|---|---|
| hospital | `desk`, `officer`, `admin`, `svc-n8n`, `svc-crew`, `svc-internal` |
| insurer | `reviewer`, `approver`, `admin`, `svc-n8n`, `svc-crew`, `svc-internal` |

Composite: `admin` does NOT include `officer`/`approver` (separation of duties; admin manages config but does not decide claims). Approver can do everything reviewer can (composite `approver` → `reviewer`).

#### 3.3.3 Clients per realm (`<sys>` = hospital|insurer)
| Client ID | Type | Flow | Notes |
|---|---|---|---|
| `<sys>-ui` | public | auth code + PKCE (S256) | redirect `http://localhost:3000/*` (hospital) or `3100/*` (insurer); web origins same; no implicit |
| `<sys>-api` | bearer-only | n/a | audience for access tokens; holds role definitions |
| `<sys>-n8n` | confidential | client credentials | service account has role `svc-n8n` |
| `<sys>-crew` | confidential | client credentials | role `svc-crew` |
| `<sys>-internal` | confidential | client credentials | used by doc-pipeline/vision callbacks to `hospital-api /internal/*`; role `svc-internal` |

#### 3.3.4 Token claims (protocol mappers)
| Claim | Source |
|---|---|
| `sub`, `preferred_username`, `email`, `exp`, `iat`, `iss` | standard |
| `realm_access.roles` | realm roles mapper |
| `aud` | audience mapper → `<sys>-api` |
| `system` | hardcoded claim mapper `hospital` / `insurer` |
| `hospital_id` (hospital realm only) | user attribute mapper (a Desk user is bound to one hospital; PROPOSED single-hospital demo) |

Lifetimes: access 5 min, refresh (UI) 30 min idle / 8 h max, service accounts access 10 min. `Not before` revocation allowed.

#### 3.3.5 Demo users (dev only; passwords from `.env`, never committed in realm JSON as plain text — use `${DEMO_PW}` placeholder replaced by `render-realms.sh`)
| Realm | Username | Roles |
|---|---|---|
| hospital | `desk1` | desk |
| hospital | `officer1`, `officer2` | officer |
| hospital | `hadmin` | admin |
| insurer | `reviewer1`, `reviewer2` | reviewer |
| insurer | `approver1`, `approver2`, `approver3` | approver (three, so dual approval above `T_four` has two distinct approvers plus a spare) |
| insurer | `iadmin` | admin |

## 4. API / endpoints

| Service | Endpoint | Notes |
|---|---|---|
| Redis | `redis:6379` | ACL auth, no TLS in dev |
| MinIO S3 | `http://minio:9000` | S3 API |
| MinIO console | `http://localhost:9001` | admin only, bound to 127.0.0.1 |
| ClamAV | `clamav:3310` | clamd TCP, INSTREAM |
| Keycloak OIDC discovery | `http://keycloak:8080/realms/{realm}/.well-known/openid-configuration` | |
| Keycloak JWKS | `/realms/{realm}/protocol/openid-connect/certs` | cached 10 min |
| Keycloak token | `/realms/{realm}/protocol/openid-connect/token` | client credentials for services |
| Keycloak health | `http://keycloak:9000/health/ready` | management port with `KC_HEALTH_ENABLED=true` |

Example: service token fetch (n8n, crew)
```
POST /realms/hospital/protocol/openid-connect/token
grant_type=client_credentials&client_id=hospital-n8n&client_secret=...
-> {"access_token":"eyJ...","expires_in":600,"token_type":"Bearer"}
```
Example decoded access token (hospital officer):
```json
{"iss":"http://keycloak:8080/realms/hospital","aud":"hospital-api","sub":"7c3...","preferred_username":"officer1",
 "realm_access":{"roles":["officer"]},"system":"hospital","hospital_id":"H-001","exp":1790000000}
```

Shared helper policy: no shared runtime library for infra. Dev A writes `hospital/api/app/infra/{storage,scan,auth,redis_client}.py`; Dev B copies the files to `insurer/api/app/infra/` and changes only the namespace constants. A small test (`contract/tests/test_infra_parity.py`) diffs the two copies ignoring a whitelisted constants block and fails if they drift.

## 5. Build tasks

### Task 1 — compose file `infra/compose/shared.yml`
Full content:
```yaml
name: claims
x-healthy: &hc {interval: 10s, timeout: 5s, retries: 10}

services:
  redis:
    image: redis:7.4-alpine
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
    environment: {REDIS_PASSWORD: "${REDIS_PASSWORD}"}
    volumes:
      - redis-data:/data
      - ../redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
      - ../redis/users.acl:/usr/local/etc/redis/users.acl:ro
    mem_limit: 256m
    healthcheck: {<<: *hc, test: ["CMD-SHELL","redis-cli -a \"$$REDIS_PASSWORD\" --no-auth-warning ping | grep PONG"]}
    networks: [claims]
    profiles: [infra]

  minio:
    image: minio/minio:RELEASE.2025-01-20T14-49-07Z   # pin; PROPOSED
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      MINIO_KMS_SECRET_KEY: "local-key:${MINIO_KMS_KEY_B64}"
      MINIO_KMS_AUTO_ENCRYPTION: "on"
    volumes: [minio-data:/data]
    ports: ["127.0.0.1:9000:9000","127.0.0.1:9001:9001"]
    mem_limit: 512m
    healthcheck: {<<: *hc, test: ["CMD","mc","ready","local"]}
    networks: [claims]
    profiles: [infra]

  minio-init:
    image: minio/mc:RELEASE.2025-01-17T23-25-50Z
    depends_on: {minio: {condition: service_healthy}}
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      HOSP_MINIO_SECRET: ${HOSP_MINIO_SECRET}
      INS_MINIO_SECRET: ${INS_MINIO_SECRET}
      DOCPIPE_HOSP_MINIO_SECRET: ${DOCPIPE_HOSP_MINIO_SECRET}
      DOCPIPE_INS_MINIO_SECRET: ${DOCPIPE_INS_MINIO_SECRET}
      VISION_MINIO_SECRET: ${VISION_MINIO_SECRET}
      RAG_MINIO_SECRET: ${RAG_MINIO_SECRET}
      ANCHOR_MINIO_SECRET: ${ANCHOR_MINIO_SECRET}
    entrypoint: ["/bin/sh","/init.sh"]
    volumes:
      - ../minio/init.sh:/init.sh:ro
      - ../minio/policies:/policies:ro
    restart: "no"
    networks: [claims]
    profiles: [infra]

  clamav:
    image: clamav/clamav:1.4
    volumes:
      - clamav-db:/var/lib/clamav
      - ../clamav/clamd.conf:/etc/clamav/clamd.conf:ro
      - ../clamav/freshclam.conf:/etc/clamav/freshclam.conf:ro
    mem_limit: 1536m
    healthcheck: {test: ["CMD","clamdcheck.sh"], interval: 30s, timeout: 10s, retries: 10, start_period: 240s}
    networks: [claims]
    profiles: [infra]

  keycloak:
    image: quay.io/keycloak/keycloak:25.0
    command: ["start-dev","--import-realm"]
    environment:
      KC_DB: dev-file
      KC_HEALTH_ENABLED: "true"
      KC_HOSTNAME_STRICT: "false"
      KC_HTTP_PORT: 8080
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: ${KC_ADMIN_PW}
    volumes:
      - ../keycloak/rendered:/opt/keycloak/data/import:ro
      - keycloak-data:/opt/keycloak/data/h2
    ports: ["127.0.0.1:8080:8080"]
    mem_limit: 768m
    healthcheck: {test: ["CMD-SHELL","exec 3<>/dev/tcp/127.0.0.1/9000; echo -e 'GET /health/ready HTTP/1.1\\r\\nhost: l\\r\\nConnection: close\\r\\n\\r\\n' >&3; grep -q UP <&3"], interval: 15s, retries: 20, start_period: 60s}
    networks: [claims]
    profiles: [infra]

volumes: {redis-data: {}, minio-data: {}, clamav-db: {}, keycloak-data: {}}
networks: {claims: {name: claims}}
```
Dev-mode note (PROPOSED): `start-dev` + H2 is acceptable on localhost; document that production needs Postgres and `start --optimized`. Because H2 persists in `keycloak-data`, re-import only happens when the realm does not exist; use `make kc-reset` (drops the volume) after editing realm JSON.

### Task 2 — Redis config
`infra/redis/redis.conf`:
```
bind 0.0.0.0
protected-mode yes
port 6379
aclfile /usr/local/etc/redis/users.acl
appendonly yes
appendfsync everysec
save 300 10
maxmemory 200mb
maxmemory-policy volatile-lru
notify-keyspace-events ""
databases 8
tcp-keepalive 60
timeout 0
loglevel notice
```
`infra/redis/users.acl` (template rendered by `render-acl.sh` replacing `${...}`):
```
user default off
user admin on >${REDIS_PASSWORD} ~* &* +@all
user hosp_app on >${HOSP_REDIS_PW} ~idem:hosp:* ~rl:hosp:* ~lock:hosp:* ~sse:hospital:* ~docpipe:* ~vision:* &sse:hospital:* &cfg:changed &audit:appended +@all -@dangerous -@admin +ping +info
user ins_app  on >${INS_REDIS_PW}  ~idem:ins:*  ~rl:ins:*  ~lock:ins:*  ~sse:insurer:*  &sse:insurer:*  &cfg:changed &audit:appended +@all -@dangerous -@admin +ping +info
user llm      on >${LLM_REDIS_PW}  ~llm:* +@all -@dangerous -@admin
user n8n_hosp on >${N8N_HOSP_REDIS_PW} ~bull:* ~n8n:* +@all -@dangerous -@admin
user n8n_ins  on >${N8N_INS_REDIS_PW}  ~bull:* ~n8n:* +@all -@dangerous -@admin
user docpipe  on >${DOCPIPE_REDIS_PW}  ~docpipe:* ~lock:docpipe:* +@all -@dangerous -@admin
```
Caveat: `n8n_hosp` and `n8n_ins` share key patterns (`bull:*`) — isolation is achieved by DB index (n8n config `QUEUE_BULL_REDIS_DB=0` vs `1`). Documented in the compose comments. `-@dangerous` removes `KEYS`, `FLUSHALL`, `CONFIG`; n8n/Bull use `SCAN` which is permitted.

### Task 3 — MinIO init
`infra/minio/init.sh` (idempotent; every command tolerates "already exists"):
```sh
#!/bin/sh
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

for b in hospital-docs insurer-docs kb-sources; do
  mc mb --ignore-existing local/$b
  mc version enable local/$b
  mc encrypt set sse-s3 local/$b
done
mc mb --ignore-existing --with-lock local/audit-anchors
mc retention set --default COMPLIANCE 365d local/audit-anchors
mc encrypt set sse-s3 local/audit-anchors

# lifecycle
for b in hospital-docs insurer-docs; do
  mc ilm rule add --noncurrent-expire-days 30 local/$b || true
  mc ilm rule add --expire-delete-marker local/$b || true
done

mk_user() { # name secret policy-file policy-name
  mc admin user add local "$1" "$2" || true
  mc admin policy create local "$4" "/policies/$3" || mc admin policy update local "$4" "/policies/$3" || true
  mc admin policy attach local "$4" --user "$1" || true
}
mk_user hosp_svc     "$HOSP_MINIO_SECRET"          hosp-rw.json         hosp-rw
mk_user ins_svc      "$INS_MINIO_SECRET"           ins-rw.json          ins-rw
mk_user docpipe_hosp "$DOCPIPE_HOSP_MINIO_SECRET"  docpipe-ro-hosp.json docpipe-hosp
mk_user docpipe_ins  "$DOCPIPE_INS_MINIO_SECRET"   docpipe-ro-ins.json  docpipe-ins
mk_user vision_svc   "$VISION_MINIO_SECRET"        vision-ro.json       vision
mk_user rag_svc      "$RAG_MINIO_SECRET"           rag-ro.json          rag
mk_user anchor_svc   "$ANCHOR_MINIO_SECRET"        anchor-w.json        anchor
echo "minio-init done"
```

### Task 4 — ClamAV config
`infra/clamav/clamd.conf`:
```
LogFile /var/log/clamav/clamd.log
LogTime yes
TCPSocket 3310
TCPAddr 0.0.0.0
MaxThreads 4
StreamMaxLength 30M
MaxFileSize 30M
MaxScanSize 120M
MaxRecursion 10
MaxFiles 500
ReadTimeout 60
CommandReadTimeout 30
PCREMaxFileSize 30M
AlertEncrypted yes
AlertEncryptedArchive yes
AlertEncryptedDoc yes
DetectPUA yes
ScanPDF yes
ScanOLE2 yes
ScanArchive yes
```
`AlertEncrypted*` makes password-protected files return `FOUND Heuristics.Encrypted.PDF` etc., which the upload path maps to `422 encrypted_file` (distinct from `infected_file`).
`infra/clamav/freshclam.conf`: `DatabaseMirror database.clamav.net`, `Checks 12`, `DatabaseDirectory /var/lib/clamav`. Volume `clamav-db` persists signatures so restarts do not re-download (~300 MB).

### Task 5 — Keycloak realms
Source templates in `infra/keycloak/templates/realm-hospital.json.tpl` and `realm-insurer.json.tpl`; `infra/keycloak/render-realms.sh` substitutes secrets (client secrets, demo passwords) from `.env` into `infra/keycloak/rendered/` (gitignored).

Skeleton for `realm-hospital.json.tpl` (the insurer file is identical with role/client names swapped and ports 3100):
```json
{
  "realm": "hospital", "enabled": true, "sslRequired": "none",
  "accessTokenLifespan": 300, "ssoSessionIdleTimeout": 1800, "ssoSessionMaxLifespan": 28800,
  "bruteForceProtected": true, "failureFactor": 5, "registrationAllowed": false,
  "roles": {"realm": [
    {"name":"desk"},{"name":"officer"},{"name":"admin"},
    {"name":"svc-n8n"},{"name":"svc-crew"},{"name":"svc-internal"}]},
  "clients": [
    {"clientId":"hospital-ui","publicClient":true,"standardFlowEnabled":true,"directAccessGrantsEnabled":false,
     "redirectUris":["http://localhost:3000/*"],"webOrigins":["http://localhost:3000"],
     "attributes":{"pkce.code.challenge.method":"S256"},
     "protocolMappers":[
       {"name":"aud-api","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper",
        "config":{"included.client.audience":"hospital-api","access.token.claim":"true"}},
       {"name":"system","protocol":"openid-connect","protocolMapper":"oidc-hardcoded-claim-mapper",
        "config":{"claim.name":"system","claim.value":"hospital","jsonType.label":"String","access.token.claim":"true"}},
       {"name":"hospital_id","protocol":"openid-connect","protocolMapper":"oidc-usermodel-attribute-mapper",
        "config":{"user.attribute":"hospital_id","claim.name":"hospital_id","access.token.claim":"true"}}]},
    {"clientId":"hospital-api","bearerOnly":true},
    {"clientId":"hospital-n8n","secret":"${HOSP_N8N_CLIENT_SECRET}","serviceAccountsEnabled":true,"publicClient":false,
     "standardFlowEnabled":false,"directAccessGrantsEnabled":false},
    {"clientId":"hospital-crew","secret":"${HOSP_CREW_CLIENT_SECRET}","serviceAccountsEnabled":true},
    {"clientId":"hospital-internal","secret":"${HOSP_INTERNAL_CLIENT_SECRET}","serviceAccountsEnabled":true}],
  "users": [
    {"username":"desk1","enabled":true,"credentials":[{"type":"password","value":"${DEMO_PW}","temporary":false}],
     "realmRoles":["desk"],"attributes":{"hospital_id":["H-001"]}},
    {"username":"officer1","realmRoles":["officer"],"enabled":true,"credentials":[{"type":"password","value":"${DEMO_PW}"}],"attributes":{"hospital_id":["H-001"]}},
    {"username":"officer2","realmRoles":["officer"],"enabled":true,"credentials":[{"type":"password","value":"${DEMO_PW}"}],"attributes":{"hospital_id":["H-001"]}},
    {"username":"hadmin","realmRoles":["admin"],"enabled":true,"credentials":[{"type":"password","value":"${DEMO_PW}"}]},
    {"username":"service-account-hospital-n8n","serviceAccountClientId":"hospital-n8n","realmRoles":["svc-n8n"],"enabled":true},
    {"username":"service-account-hospital-crew","serviceAccountClientId":"hospital-crew","realmRoles":["svc-crew"],"enabled":true},
    {"username":"service-account-hospital-internal","serviceAccountClientId":"hospital-internal","realmRoles":["svc-internal"],"enabled":true}]
}
```
Service-account clients also need the audience mapper to `hospital-api`; add the same `aud-api` mapper to each of them (otherwise the API rejects with `aud` mismatch). Insurer realm: roles `reviewer, approver, admin, svc-*`; composite `approver` includes `reviewer` (use `"composites":{"realm":["reviewer"]}`); users per §3.3.5; UI redirect `http://localhost:3100/*`.

### Task 6 — Keycloak README
`infra/keycloak/README.md`: role matrix (who may call what, linked to API docs), demo credentials location, how to add a user, `make kc-export` (runs `kc.sh export --realm hospital --dir /tmp/export`), `make kc-reset`.

### Task 7 — Smoke script `infra/smoke.sh`
Checks, in order, each printing PASS/FAIL and exiting non-zero on first failure:
1. Redis: `PING` with `hosp_app`; verify `GET idem:ins:x` returns `NOPERM`.
2. MinIO: put a 1 KB object into `hospital-docs` as `hosp_svc`; `mc stat` shows `X-Amz-Server-Side-Encryption: AES256`; `ins_svc` GET returns 403.
3. ClamAV: `PING` → `PONG`; INSTREAM with the EICAR string → `FOUND`; INSTREAM with a clean PDF → `OK`.
4. Keycloak: fetch discovery for both realms; token via password grant for `desk1` (enable direct grant on a temporary `smoke` client OR use client-credentials for `hospital-n8n`, PROPOSED the latter); decode token and assert `iss`, `aud`, `roles`, `system`.
5. Cross-realm: send a hospital token to the verification function configured with the insurer issuer → must be rejected.

### Task 8 — Python helper files (`hospital/api/app/infra/`)
Full code skeletons:

`storage.py`
```python
import hashlib, boto3
from botocore.config import Config
from app.settings import settings

_s3 = boto3.client(
    "s3",
    endpoint_url=settings.minio_url,
    aws_access_key_id=settings.minio_user,
    aws_secret_access_key=settings.minio_secret,
    config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
)


def put_stream(bucket: str, key: str, fileobj, *, tags: dict[str, str] | None = None) -> str:
    """Streams to MinIO while computing sha256; returns hex digest."""
    h = hashlib.sha256()

    class _Tee:
        def read(self, n=-1):
            b = fileobj.read(n)
            h.update(b)
            return b

    _s3.upload_fileobj(_Tee(), bucket, key, ExtraArgs={"Tagging": _enc_tags(tags or {})})
    digest = h.hexdigest()
    _s3.put_object_tagging(
        Bucket=bucket,
        Key=key,
        Tagging={
            "TagSet": [
                {"Key": k, "Value": v} for k, v in {**(tags or {}), "sha256": digest}.items()
            ]
        },
    )
    return digest


def presign_get(bucket: str, key: str, ttl: int = 86400, filename: str | None = None) -> str:
    params = {"Bucket": bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return _s3.generate_presigned_url("get_object", Params=params, ExpiresIn=min(ttl, 72 * 3600))
```
`scan.py`
```python
import socket, struct
from dataclasses import dataclass


@dataclass
class ScanResult:
    status: str  # "clean" | "infected" | "encrypted" | "error"
    signature: str | None = None


def scan_stream(f, host: str, port: int, timeout: float = 30.0) -> ScanResult:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"zINSTREAM\0")
            while chunk := f.read(8192):
                s.sendall(struct.pack(">I", len(chunk)) + chunk)
            s.sendall(struct.pack(">I", 0))
            resp = s.recv(4096).decode().strip("\0\n ")
    except (OSError, socket.timeout):
        return ScanResult("error")
    if resp.endswith("OK"):
        return ScanResult("clean")
    if "FOUND" in resp:
        sig = resp.split(":")[-1].replace("FOUND", "").strip()
        return ScanResult("encrypted" if "Encrypted" in sig else "infected", sig)
    return ScanResult("error")
```
`auth.py`
```python
import time, httpx
from jose import jwt, JWTError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer

_bearer = HTTPBearer(auto_error=False)
_jwks = {"keys": [], "fetched": 0.0}


async def _get_jwks(force=False):
    if force and time.time() - _jwks["fetched"] < 60:
        force = False  # at most 1 forced refresh / 60 s
    if force or time.time() - _jwks["fetched"] > 600 or not _jwks["keys"]:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.kc_realm_url}/protocol/openid-connect/certs")
            r.raise_for_status()
        _jwks.update(keys=r.json()["keys"], fetched=time.time())
    return _jwks["keys"]


async def current_user(cred=Depends(_bearer)) -> User:
    if not cred:
        raise HTTPException(401, "missing_token")
    try:
        header = jwt.get_unverified_header(cred.credentials)
        keys = await _get_jwks()
        key = next((k for k in keys if k["kid"] == header["kid"]), None)
        if key is None:
            keys = await _get_jwks(force=True)
            key = next((k for k in keys if k["kid"] == header["kid"]), None)
        if key is None:
            raise HTTPException(401, "unknown_kid")
        claims = jwt.decode(
            cred.credentials,
            key,
            algorithms=["RS256"],
            audience=settings.api_audience,
            issuer=settings.kc_realm_url,
        )
    except JWTError as e:
        raise HTTPException(401, "invalid_token") from e
    if claims.get("system") != settings.system_name:
        raise HTTPException(401, "wrong_system")
    return User(
        sub=claims["sub"],
        username=claims.get("preferred_username"),
        roles=set(claims["realm_access"]["roles"]),
        hospital_id=claims.get("hospital_id"),
    )


def require_roles(*allowed):
    def dep(u: User = Depends(current_user)):
        if not (u.roles & set(allowed)):
            raise HTTPException(403, "forbidden")
        return u

    return dep
```
`redis_client.py`: `redis.asyncio.Redis.from_url(settings.redis_url)` with ACL user; helper `with_lock(resource)` using `SET NX PX` and a Lua compare-and-delete release script.

### Task 9 — RAM budget `infra/compose/README.md`
| Service | mem_limit | Typical |
|---|---|---|
| redis | 256 MB | 40 MB |
| minio | 512 MB | 200 MB |
| clamav | 1536 MB | 1.1 GB (signatures in RAM) |
| keycloak | 768 MB | 550 MB |
| **Total** | **3.07 GB** | ~1.9 GB |

### Task 10 — Makefile targets
`up-infra` (`docker compose -f infra/compose/shared.yml --profile infra up -d --wait`), `down-infra`, `smoke`, `kc-export`, `kc-reset`, `render-secrets` (generates `.env` random secrets from `.env.example` for first run using `openssl rand -hex 24`).

## 6. Key logic / pseudocode

### 6.1 Upload path (hospital-api, restated for infra expectations)
```
async def upload(file, case_id):
    spool = SpooledTemporaryFile(max_size=2MB); copy with size cap MAX_UPLOAD_MB -> 413 if exceeded
    sniff magic bytes (pdf/png/jpeg/tiff only) -> 415 otherwise
    res = scan_stream(spool.rewind())
    if res.status == "infected": audit(doc.scanned, infected, sig) ; raise 422 infected_file     # not stored
    if res.status == "encrypted": raise 422 encrypted_file
    if res.status == "error":   raise 503 scanner_unavailable (Retry-After: 30)                  # fail closed
    sha = put_stream("hospital-docs", f"{case_id}/{doc_id}/original.{ext}", spool.rewind(), tags={"scan":"clean"})
    audit(doc.uploaded) ; audit(doc.scanned, clean)
```
### 6.2 JWT validation
As in `auth.py`; additional invariants: `exp` required; leeway 30 s; `alg` pinned to RS256 (reject `none`/HS*); `iss` equal to the configured realm URL (NOT derived from the token) so cross-realm tokens fail; `aud` contains the API client; `system` equals the service's own name.

### 6.3 Presign and download by the insurer
Hospital: `presign_get(bucket, key, ttl=86400)`. Insurer downloads with `httpx` streaming, checks `Content-Length` ≤ 30 MB, computes sha256 while streaming, compares to `DocumentRef.sha256` (mismatch → `doc_unavailable` and audit), then runs its own ClamAV scan before writing to `insurer-docs`.

### 6.4 Idempotency (Redis) reference
```
def claim_idem(key_id, idem, req_hash):
    k = f"idem:{SYS}:{key_id}:{idem}"
    if redis.hsetnx(k, "req_hash", req_hash):  redis.expire(k, 86400); return NEW
    stored = redis.hgetall(k)
    return REPLAY(stored) if stored["req_hash"] == req_hash and "status" in stored else CONFLICT_or_INFLIGHT
```
In-flight duplicates (hash set, status not yet) return 409 `request_in_progress` with `Retry-After: 2`.

## 7. Config / env vars
| Var | Used by | Example |
|---|---|---|
| `REDIS_PASSWORD` | admin / healthcheck | random |
| `HOSP_REDIS_PW`, `INS_REDIS_PW`, `LLM_REDIS_PW`, `N8N_HOSP_REDIS_PW`, `N8N_INS_REDIS_PW`, `DOCPIPE_REDIS_PW` | ACL users | random |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | init only | |
| `MINIO_KMS_KEY_B64` | SSE-S3 key (32 random bytes base64) | |
| `HOSP_MINIO_SECRET`, `INS_MINIO_SECRET`, `DOCPIPE_HOSP_MINIO_SECRET`, `DOCPIPE_INS_MINIO_SECRET`, `VISION_MINIO_SECRET`, `RAG_MINIO_SECRET`, `ANCHOR_MINIO_SECRET` | service users | random |
| `KC_ADMIN_PW`, `DEMO_PW` | Keycloak | |
| `HOSP_N8N_CLIENT_SECRET`, `HOSP_CREW_CLIENT_SECRET`, `HOSP_INTERNAL_CLIENT_SECRET`, `INS_N8N_CLIENT_SECRET`, `INS_CREW_CLIENT_SECRET`, `INS_INTERNAL_CLIENT_SECRET` | client credentials | |
| `KC_HOSPITAL_REALM_URL=http://keycloak:8080/realms/hospital`, `KC_INSURER_REALM_URL=...insurer` | APIs | |
| `CLAMAV_HOST=clamav`, `CLAMAV_PORT=3310`, `MAX_UPLOAD_MB=25` | APIs | |
| `PRESIGN_TTL_SECONDS=86400` | hospital-api | |

Secret hygiene: `.env` is gitignored; `make render-secrets` generates; gitleaks pre-commit blocks accidental commits; realm JSON committed only as templates.

## 8. Error handling and edge cases
| # | Situation | Behaviour |
|---|---|---|
| 1 | ClamAV still loading signatures (first boot up to 4 min) | `scan_stream` returns `error` → API 503 `scanner_unavailable` + `Retry-After`; UI shows "scanner warming up, retry"; NEVER skip scanning |
| 2 | File larger than `StreamMaxLength` | clamd returns `INSTREAM size limit exceeded`; API pre-checks size and returns 413 before scanning; clamd limit (30 MB) > `MAX_UPLOAD_MB` (25) |
| 3 | Encrypted PDF / zip | 422 `encrypted_file`; UI guides user to upload an unprotected copy |
| 4 | Zip bomb | `MaxScanSize`/`MaxRecursion` trips → clamd FOUND `Heuristics.Limits.Exceeded` → treated as infected |
| 5 | Redis down | idempotency falls back to DB table for claim submissions; SSE degrades to 10 s polling; rate limiter fails open with a log; locks unavailable → operations that need them return 503 |
| 6 | Redis `NOPERM` from a missing ACL pattern | surfaces as startup self-test failure (each service runs `redis_selftest()` that writes/reads its own prefix) |
| 7 | MinIO presigned URL clock skew | all containers share host clock; presign TTL min 10 min; insurer treats 403 `RequestTimeTooSkewed` as `doc_unavailable` and requests refresh |
| 8 | MinIO disk full | PUT fails 507-ish; API returns 503 `storage_unavailable`; alert R-series in runbook (05-04) |
| 9 | Duplicate upload (same sha256 in same case) | API returns existing `doc_id` with `duplicate=true` (decided in hospital doc 03) |
| 10 | Keycloak restart | H2 volume persists sessions; if volume lost, realm re-imports from JSON, users re-login |
| 11 | JWKS rotation | `kid` miss triggers one forced refresh per 60 s |
| 12 | Keycloak down | existing valid tokens still verify from cached JWKS up to 10 min+ (cache not expired on fetch failure: keep stale keys up to 1 h, log warning); new logins fail |
| 13 | Service account token expiry mid-workflow (n8n) | n8n credential uses automatic refresh; on 401 retry once with fresh token |
| 14 | Wrong-realm token | 401 `invalid_token` (issuer mismatch) |
| 15 | Clock drift between Keycloak and API | 30 s leeway; beyond that, 401 `invalid_token` and runbook entry |
| 16 | Object overwritten | versioning on; `parsed.json` rewrites create new versions (idempotent pipeline) |
| 17 | EICAR in test | must be rejected; test file is generated in code (not committed, to avoid AV on dev machines) |

## 9. Tests

### 9.1 Smoke (`infra/smoke.sh`) — per Task 7.

### 9.2 Unit tests (`hospital/api/tests/infra/`)
| Test | Detail |
|---|---|
| `test_storage_put_stream_hash` | digest equals independent sha256; tags set |
| `test_storage_presign_ttl_cap` | requests of 200 h are capped at 72 h |
| `test_scan_clean/infected/encrypted/error` | fake clamd socket server returns `stream: OK`, `stream: Eicar-Test-Signature FOUND`, `stream: Heuristics.Encrypted.PDF FOUND`, closes connection |
| `test_scan_chunking` | 100 KB input sends correct 4-byte length prefixes and terminator |
| `test_auth_valid` | RSA keypair generated in test, JWKS served by `respx` |
| `test_auth_expired/wrong_issuer/wrong_audience/wrong_system/none_alg/hs256_confusion/missing_roles` | all 401 |
| `test_auth_kid_rotation` | unknown kid triggers one refresh; second within 60 s does not |
| `test_require_roles` | 403 for wrong role |
| `test_redis_lock` | second acquirer fails; release by non-owner no-ops |
| `test_idem_flow` | NEW, REPLAY, CONFLICT, INFLIGHT |

### 9.3 ACL tests (compose-based)
`hosp_app` cannot read `idem:ins:*` (NOPERM); cannot publish to `sse:insurer:*`; `FLUSHALL`/`KEYS` denied.

### 9.4 Policy tests
`ins_svc` 403 on `hospital-docs` GET/PUT/LIST; `hosp_svc` 403 on `insurer-docs`; `docpipe_hosp` cannot overwrite `original.pdf` but can write `parsed.json`; `anchor_svc` cannot delete from `audit-anchors` (object lock).

### 9.5 Parity test
`contract/tests/test_infra_parity.py`: normalises both `infra/` helper directories and diffs them.

### 9.6 Resilience tests
Stop ClamAV → upload returns 503 and nothing is stored; stop Redis → claim submission still idempotent through DB; restart Keycloak → APIs still validate cached tokens.

## 10. Acceptance criteria
- [ ] `make up-infra` all healthy in < 5 min on first boot (ClamAV signature download dominating), < 90 s afterwards; total RSS for the four services < 2.5 GB.
- [ ] Both realms import cleanly; every demo user logs in; token claims match §3.3.4.
- [ ] EICAR rejected; clean PDF stored with SSE (`mc stat` shows encryption); encrypted PDF rejected as `encrypted_file`.
- [ ] Cross-bucket and cross-prefix Redis/MinIO access denied per §9.3-9.4.
- [ ] Hospital token rejected by insurer helper and vice versa.
- [ ] `smoke.sh` passes from a clean clone with only `.env` generated.
- [ ] No secrets in git (gitleaks green).

## 11. Dependencies
Blocks: hospital-api (02-dev-A docs 02, 03), insurer-api (Dev B copies helpers), n8n (queue + service accounts), doc-pipeline and vision-service (MinIO read users, Redis), rag-service (kb-sources). Depends on: `05-repo-layout-and-conventions.md` (compose profiles, env naming), `01-04-audit-hash-chain.md` (audit-anchors bucket and `audit:appended` channel).

## 12. Claude Code kickoff prompt
> Read docs/implementation/01-shared-contract/05-repo-layout-and-conventions.md and docs/implementation/04-shared-services/01-infra-redis-minio-clamav-keycloak.md. Implement tasks 1-10 in order. After each service (Redis, MinIO, ClamAV, Keycloak) extend infra/smoke.sh and run it. Generate the EICAR string in test code, never commit it. Do not change contract/. Keep realm JSON as templates with placeholders. Stop at the acceptance criteria and report which boxes are checked.
