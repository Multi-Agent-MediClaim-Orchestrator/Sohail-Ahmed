"""Shared pytest fixtures: ``pg_server`` (docker postgres), ``pg_db`` (fresh database per test module)."""

import pytest

from support.pg import PgServer, docker_available


@pytest.fixture(scope="session")
def pg_server():
    import os

    if not os.environ.get("TEST_PG_URL") and not docker_available():
        pytest.skip("docker not available and TEST_PG_URL not set")
    srv = PgServer().start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def pg_db(pg_server):
    """Fresh empty database (plain postgresql:// URL)."""
    return pg_server.create_database()


@pytest.fixture(scope="session")
def redis_url():
    """Real Redis (docker ``redis:7-alpine``) or ``TEST_REDIS_URL``."""
    import os
    import subprocess
    import time
    import uuid

    from support.pg import _free_port

    if os.environ.get("TEST_REDIS_URL"):
        yield os.environ["TEST_REDIS_URL"]
        return
    if not docker_available():
        pytest.skip("docker not available and TEST_REDIS_URL not set")
    port, name = _free_port(), f"claims-test-redis-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"{port}:6379", "redis:7-alpine"], check=True, capture_output=True)
    url = f"redis://127.0.0.1:{port}/0"
    import redis as sync_redis

    for _ in range(60):
        try:
            sync_redis.Redis.from_url(url).ping()
            break
        except Exception:
            time.sleep(0.5)
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="session")
def qdrant_url():
    """Real Qdrant (docker ``qdrant/qdrant:v1.12.4``) or ``TEST_QDRANT_URL``."""
    import os
    import subprocess
    import time
    import uuid

    import httpx

    from support.pg import _free_port

    if os.environ.get("TEST_QDRANT_URL"):
        yield os.environ["TEST_QDRANT_URL"]
        return
    if not docker_available():
        pytest.skip("docker not available and TEST_QDRANT_URL not set")
    port, name = _free_port(), f"claims-test-qdrant-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"{port}:6333", "qdrant/qdrant:v1.12.4"], check=True, capture_output=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(url + "/readyz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
