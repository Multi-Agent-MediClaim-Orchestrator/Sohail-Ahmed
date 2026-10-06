"""Render Keycloak realm import files for `hospital` and `insurer` from .env into
infra/keycloak/rendered/ (gitignored). Secrets never live in committed JSON."""

import json
import os
import pathlib

OUT = pathlib.Path(__file__).parent / "rendered"
ENV = {}
for line in pathlib.Path(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        ENV[k] = v
ENV.update({k: v for k, v in os.environ.items() if k in ENV})
PW = ENV["DEMO_PW"]
SEED_USERS = {
    u["username"]: u["sub"]
    for u in json.loads(pathlib.Path("hospital/api/seed/users.json").read_text())
}  # fixed ids so the hospital DB seed matches Keycloak


def mappers(system: str, extra: list | None = None) -> list:
    base = [
        {
            "name": "aud-api",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": f"{system}-api", "access.token.claim": "true"},
        },
        {
            "name": "system",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "system",
                "claim.value": system,
                "jsonType.label": "String",
                "access.token.claim": "true",
            },
        },
    ]
    return base + (extra or [])


def service_client(system: str, name: str, secret: str) -> dict:
    return {
        "clientId": f"{system}-{name}",
        "secret": secret,
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "protocolMappers": mappers(system),
    }


def user(name: str, roles: list[str], attrs: dict | None = None) -> dict:
    u = {
        "username": name,
        "enabled": True,
        "emailVerified": True,
        "firstName": name,
        "lastName": "Demo",
        "email": f"{name}@claims.local",
        "credentials": [{"type": "password", "value": PW, "temporary": False}],
        "realmRoles": roles,
    }
    if name in SEED_USERS:
        u["id"] = SEED_USERS[name]
    if attrs:
        u["attributes"] = attrs
    return u


def realm(system: str, ui_port: int, roles: list[dict], users: list[dict], secrets: dict) -> dict:
    hosp_attr = []
    if system == "hospital":
        hosp_attr = [
            {
                "name": "hospital_id",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-attribute-mapper",
                "config": {
                    "user.attribute": "hospital_id",
                    "claim.name": "hospital_id",
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                },
            }
        ]
    clients = [
        {
            "clientId": f"{system}-ui",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "redirectUris": [f"http://localhost:{ui_port}/*"],
            "webOrigins": [f"http://localhost:{ui_port}"],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": mappers(system, hosp_attr),
        },
        {"clientId": f"{system}-api", "bearerOnly": True},
    ]
    sa_users = []
    for name, role in (("n8n", "svc-n8n"), ("crew", "svc-crew"), ("internal", "svc-internal")):
        clients.append(service_client(system, name, secrets[name]))
        sa_users.append(
            {
                "username": f"service-account-{system}-{name}",
                "enabled": True,
                "serviceAccountClientId": f"{system}-{name}",
                "realmRoles": [role],
            }
        )
    return {
        "realm": system,
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 28800,
        "bruteForceProtected": True,
        "failureFactor": 5,
        "roles": {"realm": roles},
        "clients": clients,
        "users": users + sa_users,
    }


SVC = [{"name": n} for n in ("svc-n8n", "svc-crew", "svc-internal")]
HOSP = realm(
    "hospital",
    3000,
    [{"name": "desk"}, {"name": "officer"}, {"name": "admin"}, *SVC],
    [
        user("desk1", ["desk"], {"hospital_id": ["HOSP-0001"]}),
        user("officer1", ["officer"], {"hospital_id": ["HOSP-0001"]}),
        user("officer2", ["officer"], {"hospital_id": ["HOSP-0001"]}),
        user("hadmin", ["admin"]),
    ],
    {
        "n8n": ENV["HOSP_N8N_CLIENT_SECRET"],
        "crew": ENV["HOSP_CREW_CLIENT_SECRET"],
        "internal": ENV["HOSP_INTERNAL_CLIENT_SECRET"],
    },
)
INS = realm(
    "insurer",
    3100,
    [
        {"name": "reviewer"},
        {"name": "approver", "composite": True, "composites": {"realm": ["reviewer"]}},
        {"name": "admin"},
        *SVC,
    ],
    [
        user("reviewer1", ["reviewer"]),
        user("reviewer2", ["reviewer"]),
        user("approver1", ["approver"]),
        user("approver2", ["approver"]),
        user("approver3", ["approver"]),
        user("iadmin", ["admin"]),
    ],
    {
        "n8n": ENV["INS_N8N_CLIENT_SECRET"],
        "crew": ENV["INS_CREW_CLIENT_SECRET"],
        "internal": ENV["INS_INTERNAL_CLIENT_SECRET"],
    },
)
OUT.mkdir(exist_ok=True)
for r in (HOSP, INS):
    (OUT / f"realm-{r['realm']}.json").write_text(json.dumps(r, indent=2))
print("rendered", [p.name for p in OUT.iterdir()])
