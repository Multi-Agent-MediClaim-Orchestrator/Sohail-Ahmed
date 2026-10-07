"""Shared fixtures for insurer-api tests. DB-backed tests need docker (``@pytest.mark.integration``)."""

from __future__ import annotations

import os

import httpx
import pytest
from ins_helpers import (  # noqa: F401
    APP_PW,
    RO_PW,
    alembic_cfg,
    app_url,
    migrate,
    register_claim_docs,
    unique_stay,
)


@pytest.fixture(scope="module")
def migrated_db(pg_server):
    """Fresh database migrated to head (owner = postgres superuser). Returns the plain postgresql:// URL."""
    url = pg_server.create_database()
    migrate(url)
    return url


@pytest.fixture(scope="module")
def seeded_db(migrated_db):
    from seeds.seed import seed

    seed(migrated_db)
    from ins_helpers import reset_members

    reset_members()  # a fresh database starts with every seeded member unused
    return migrated_db


class Env:
    """Everything a test needs: app, hospital simulator, fake MinIO/doc server, helpers to run jobs and the outbox."""

    def __init__(self) -> None:
        self.docs: dict[str, bytes] = {}
        self.doc_status: dict[str, int] = {}

    def client(self, sub: str = "reviewer1", roles: list[str] | None = None) -> httpx.AsyncClient:
        """httpx client against the insurer app authenticated as a staff user (dev HS256 token)."""
        from app.security.auth import make_dev_token

        tok = make_dev_token(sub, roles or ["reviewer"])
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://insurer", headers={"Authorization": f"Bearer {tok}"})

    def svc(self) -> httpx.AsyncClient:
        from app.security.auth import make_dev_token

        tok = make_dev_token("svc-n8n-insurer", ["n8n-service"])
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://insurer", headers={"Authorization": f"Bearer {tok}"})


@pytest.fixture
async def env(seeded_db):
    from app import clock, db
    from app.config.service import ConfigService
    from app.main import create_app
    from app.services import docs_fetch, events, jobs
    from app.settings import get_settings
    from hospital_sim import HospitalSim

    get_settings.cache_clear()
    import json as _json

    os.environ["INS_HMAC_SECRETS"] = _json.dumps({f"hosp-{k:03d}": ["dev-hosp-to-ins-secret-0000000000000000", "previous-secret-0000000000000000000000"] for k in range(1, 13)} | {"bank-sim": ["dev-bank-sim-callback-secret-00000000000"]})
    os.environ["INS_ALLOWED_DOC_HOSTS"] = "minio:9000,hospital-minio:9000,localhost:9000"
    get_settings.cache_clear()
    db.init_db(url=app_url(seeded_db))
    jobs.clear()
    jobs.configure("manual")
    events.set_bus(events.MemoryEventBus())
    clock.set_clock(clock.Clock())
    e = Env()

    def doc_handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/")[-2] if request.url.path.count("/") >= 2 else request.url.path
        status = e.doc_status.get(key, 200)
        if status != 200:
            return httpx.Response(status)
        data = e.docs.get(key)
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)

    docs_fetch.set_deps(docs_fetch.Deps(docs_fetch.MemoryStore(), docs_fetch.EicarScanner(),
                                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(doc_handler), follow_redirects=False)))
    app = create_app(get_settings(), use_redis=False)
    e.app = app
    e.config = app.state.config
    e.sim = HospitalSim(app)
    e.sm = db.sessionmaker()
    e.settings = get_settings()
    e.owner_url = seeded_db
    yield e
    await e.sim.aclose()
    await db.dispose_db()
    _ = (ConfigService,)


