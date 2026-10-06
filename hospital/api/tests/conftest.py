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
    command.upgrade(alembic_cfg(owner_url(dbname)), "head")
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
