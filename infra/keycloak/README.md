# Keycloak (Dev A)
Two realms in one instance: `hospital` and `insurer`, rendered from `.env` by
`render_realms.py` into `rendered/` (gitignored). Different signing keys and issuers, so a token
from one realm is rejected by the other system's API.

| Realm | Roles | Demo users |
|---|---|---|
| hospital | desk, officer, admin, svc-n8n, svc-crew, svc-internal | desk1, officer1, officer2, hadmin |
| insurer | reviewer, approver (includes reviewer), admin, svc-* | reviewer1-2, approver1-3, iadmin |

Passwords: `DEMO_PW` in `.env`. Service clients (`<sys>-n8n|crew|internal`) use client credentials.
`admin` is deliberately separate from officer/approver (separation of duties).
Commands: `make render`, `make kc-reset` (re-import realms), `make test-infra`.
