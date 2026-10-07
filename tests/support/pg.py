"""Throw-away PostgreSQL (docker) for integration tests.

``TEST_PG_URL`` (e.g. ``postgresql://postgres:pw@localhost:5499/postgres``) re-uses an existing server; otherwise a
``postgres:16-alpine`` container is started on a free port and removed at the end of the session."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import suppress

import psycopg2

PASSWORD = "testpw"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


class PgServer:
    def __init__(self) -> None:
        self.url = os.environ.get("TEST_PG_URL")
        self.name: str | None = None
        self.port: int | None = None

    def start(self) -> PgServer:
        if self.url:
            return self
        self.port = _free_port()
        self.name = f"claims-test-pg-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
             "-p", f"{self.port}:5432", "postgres:16-alpine",
             "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"],
            check=True, capture_output=True,
        )
        self.url = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{self.port}/postgres"
        deadline = time.time() + 60
        while time.time() < deadline:
            with suppress(Exception):
                psycopg2.connect(self.url, connect_timeout=2).close()
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("postgres did not become ready")
        return self

    def stop(self) -> None:
        if self.name:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def create_database(self, name: str | None = None) -> str:
        """Fresh empty database; returns its SQLAlchemy-friendly base URL (postgresql://...)."""
        assert self.url
        name = name or f"t_{uuid.uuid4().hex[:10]}"
        conn = psycopg2.connect(self.url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        conn.close()
        base, _, _ = self.url.rpartition("/")
        return f"{base}/{name}"

    @staticmethod
    def async_url(url: str) -> str:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
