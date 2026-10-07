import os

import psycopg2
import pytest
from ins_helpers import alembic_cfg, migrate

pytestmark = pytest.mark.integration


def test_upgrade_downgrade_upgrade_roundtrip(pg_server):
    from alembic import command

    url = pg_server.create_database()
    migrate(url)
    cfg = alembic_cfg(url)
    command.downgrade(cfg, "base")
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('core','config','audit','ops')")
    assert cur.fetchone()[0] == 0
    conn.close()
    os.environ.setdefault("INS_APP_PASSWORD", "apppw")
    command.upgrade(cfg, "head")


@pytest.mark.parametrize("n", range(2, 14))
def test_each_migration_downgrades_one_step(pg_server, n):
    from alembic import command

    url = pg_server.create_database()
    migrate(url)
    cfg = alembic_cfg(url)
    command.downgrade(cfg, "-1") if n == 13 else None
    if n != 13:
        command.downgrade(cfg, f"{n - 1:04d}")
        command.upgrade(cfg, "head")
    else:
        command.upgrade(cfg, "head")


def test_head_has_expected_tables_and_views(migrated_db):
    conn = psycopg2.connect(migrated_db)
    cur = conn.cursor()
    cur.execute("SELECT table_schema||'.'||table_name FROM information_schema.tables WHERE table_schema IN ('core','config','audit','ops')")
    names = {r[0] for r in cur.fetchall()}
    for t in ("core.claim_case", "core.decision_task", "core.query_response", "core.settlement_task", "config.config_version",
              "audit.audit_event", "ops.outbox", "core.v_case_list", "core.v_gate_stats", "core.escalation"):
        assert t in names, t
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='core' AND table_name='v_case_list'")
    assert "submission" not in {r[0] for r in cur.fetchall()}  # heavy blob never exposed by the list view
    conn.close()
