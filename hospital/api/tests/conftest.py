"""Integration fixtures: a throw-away database migrated to head. Needs `make up-infra`."""

import pathlib
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.db.urls import owner_url, superuser_url
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

API = pathlib.Path(__file__).resolve().parents[1]


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(API / "alembic.ini"))
    cfg.set_main_option("script_location", str(API / "alembic"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def dbname():  # type: ignore[no-untyped-def]
    name = f"hosp_test_{uuid.uuid4().hex[:8]}"
    su = create_engine(superuser_url(), isolation_level="AUTOCOMMIT")
    with su.connect() as c:
        c.execute(text(f"CREATE DATABASE {name} OWNER hosp_owner"))
    yield name
    with su.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.fixture(scope="session")
def migrated(dbname: str) -> str:
    from seed.run import run as seed

    command.upgrade(alembic_cfg(owner_url(dbname)), "head")
    seed(owner_url(dbname))  # config v1, hospital, users, patients, preauth, reference data
    return dbname


@pytest.fixture
def owner(migrated: str) -> Engine:
    return create_engine(owner_url(migrated))


@pytest.fixture
def conn(owner: Engine) -> Connection:  # type: ignore[misc]
    """Owner connection inside a transaction that is rolled back after each test."""
    with owner.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()


# ---------------------------------------------------------------------------------------------
# API fixtures (real Postgres scratch DB, Redis, MinIO, ClamAV, Keycloak)
# ---------------------------------------------------------------------------------------------
import base64  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from collections.abc import AsyncIterator, Callable  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.urls import _env, app_url  # noqa: E402
from app.main import create_checked_app  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

ENVV = _env()


@pytest.fixture(scope="session")
def settings(migrated: str):  # type: ignore[no-untyped-def]
    import redis as sync_redis

    # high per-minute upload limit: tests share one real Redis counter; the limiter has its own test
    # no background outbox worker anywhere in tests: it would race the manual run_once() calls on the shared database
    s = Settings.from_env(
        db_url=app_url(migrated),
        upload_rate_per_min=100000,
        outbox_enabled=False,
        db_pool_size=3,  # several apps share one Postgres with max_connections=60
        db_max_overflow=3,
    )

    def clear_cache() -> None:  # cached user ids belong to whichever database wrote them
        r = sync_redis.Redis.from_url(s.redis_url)
        for k in r.scan_iter("cache:hosp:*"):
            r.delete(k)
        r.close()

    clear_cache()
    yield s
    clear_cache()  # do not leave ids of the throwaway test database for the dev API


async def start_app(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hospital"
        ) as c:
            yield c


@pytest_asyncio.fixture(scope="session")
async def app(settings: Settings):  # type: ignore[no-untyped-def]
    from app.events import InMemoryHub
    from app.services.n8n import RecordingN8n

    hub = InMemoryHub()
    completeness_calls: list[tuple[str, str]] = []

    async def completeness(case_id: str, reason: str) -> None:
        completeness_calls.append((case_id, reason))

    application = create_checked_app(
        settings, hub=hub, n8n=RecordingN8n(), completeness=completeness
    )
    application.state.completeness_calls = completeness_calls
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(scope="session")
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hospital"
    ) as c:
        yield c


KC = "http://localhost:8080"
_tokens: dict[str, str] = {}


def kc_user_token(username: str) -> str:
    if username not in _tokens:
        r = httpx.post(
            f"{KC}/realms/hospital/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "hospital-dev",
                "username": username,
                "password": ENVV["DEMO_PW"],
            },
        )
        assert r.status_code == 200, r.text
        _tokens[username] = r.json()["access_token"]
    return _tokens[username]


def kc_service_token(name: str) -> str:
    key = f"svc:{name}"
    if key not in _tokens:
        secret = ENVV[f"HOSP_{name.upper()}_CLIENT_SECRET"]
        r = httpx.post(
            f"{KC}/realms/hospital/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": f"hospital-{name}",
                "client_secret": secret,
            },
        )
        assert r.status_code == 200, r.text
        _tokens[key] = r.json()["access_token"]
    return _tokens[key]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def tok() -> Callable[[str], dict[str, str]]:
    """headers for a seeded realm user (desk1, officer1, officer2, hadmin) or service (svc:n8n)."""

    def get(who: str) -> dict[str, str]:
        return bearer(kc_service_token(who[4:]) if who.startswith("svc:") else kc_user_token(who))

    return get


# local RSA key + fake JWKS for the JWT validation matrix
@pytest.fixture(scope="session")
def rsa_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64u(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks_for(key: Any, kid: str = "k1") -> dict[str, Any]:
    pub = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": b64u(pub.n),
                "e": b64u(pub.e),
            }
        ]
    }


def make_token(
    key: Any,
    claims: dict[str, Any] | None = None,
    *,
    kid: str = "k1",
    alg: str = "RS256",
    drop: tuple[str, ...] = (),
) -> str:
    import jwt

    now = int(time.time())
    body: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "iss": "http://localhost:8080/realms/hospital",
        "aud": "hospital-api",
        "exp": now + 300,
        "iat": now,
        "email": "u@x.io",
        "name": "U Ser",
        "azp": "hospital-ui",
        "realm_access": {"roles": ["officer"]},
    }
    body.update(claims or {})
    for k in drop:
        body.pop(k, None)
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


@pytest_asyncio.fixture(scope="session")
async def unit_client(settings: Settings, rsa_key: Any):  # type: ignore[no-untyped-def]
    """App whose JWKS comes from a local key, so arbitrary claims can be forged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks_for(rsa_key))

    jc = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application = create_checked_app(settings, jwks_client=jc)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://hospital"
        ) as c:
            c.app = application  # type: ignore[attr-defined]
            yield c


__all__ = ["json"]


@pytest_asyncio.fixture(scope="session")
async def rapp(settings: Settings):  # type: ignore[no-untyped-def]
    """App with the REAL completeness scheduler (inline: debounce 0), recording n8n and an in-memory hub."""
    from app.events import InMemoryHub
    from app.services.n8n import RecordingN8n

    application = create_checked_app(
        settings.model_copy(update={"completeness_debounce_s": 0}),
        hub=InMemoryHub(),
        n8n=RecordingN8n(),
    )
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(scope="session")
async def rclient(rapp: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rapp), base_url="http://hospital"
    ) as c:
        yield c


def _guard_outbox_workers() -> None:
    """Every test app must start with the background outbox worker OFF. A live worker polls the shared test database
    and steals rows that tests drive by hand with run_once(): that caused flaky attempts/status failures."""
    import app.main as main_mod

    original = main_mod.create_app

    def guarded(settings: Settings | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        effective = settings or Settings.from_env()
        assert not effective.outbox_enabled, "test apps must set outbox_enabled=False"  # noqa: S101
        return original(settings, **kw)

    main_mod.create_app = guarded  # type: ignore[assignment]


_guard_outbox_workers()


import logging  # noqa: E402

for _name in ("httpx", "app.access", "httpcore"):  # keep test output readable
    logging.getLogger(_name).setLevel(logging.WARNING)


# --- claims flow: hospital app wired to insurer-sim, recording crew, worker driven manually ---------
@pytest_asyncio.fixture(scope="session")
async def capp(settings: Settings):  # type: ignore[no-untyped-def]
    from app.events import InMemoryHub
    from app.services.crew import RecordingCrew
    from app.services.n8n import RecordingN8n
    from claim_contract.testing.insurer_sim import InsurerSim

    st = settings.model_copy(update={"completeness_debounce_s": 0, "outbox_enabled": False})
    sim = InsurerSim(
        hosp_secrets={st.hospital_key_id: st.hospital_to_insurer_secret.encode()},
        callback_secret=st.insurer_to_hospital_secret.encode(),
        callback_key_id=st.insurer_key_id,
    )
    insurer_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim.app), base_url="http://insurer"
    )
    application = create_checked_app(
        st,
        hub=InMemoryHub(),
        n8n=RecordingN8n(),
        crew=RecordingCrew(),
        insurer_client=insurer_client,
    )
    application.state.sim = sim
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://hospital"
        ) as hc:
            sim.hospital = hc
            application.state.hospital_client = hc
            yield application


@pytest_asyncio.fixture(scope="session")
async def cclient(capp: Any) -> httpx.AsyncClient:
    return capp.state.hospital_client  # type: ignore[no-any-return]
