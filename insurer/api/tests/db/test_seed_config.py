from datetime import UTC, datetime

import psycopg2
import pytest
from insurer_app.config.service import ConfigNotFound, ConfigService
from insurer_app.seeds.seed import build_master, seed
from insurer_app.verification.names import name_similarity, normalise_name
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def seeded(migrated_db):
    counts = seed(migrated_db)
    return migrated_db, counts


def q(url, sql):
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute(sql)
    out = cur.fetchall()
    conn.close()
    return out


def test_seed_counts_and_idempotent(seeded):
    url, counts = seeded
    assert counts["products"] == 3 and counts["policies"] == 60 and counts["members"] >= 150 and counts["hospitals"] == 12
    before = q(url, "SELECT (SELECT count(*) FROM core.policy), (SELECT count(*) FROM core.policy_member), (SELECT count(*) FROM config.config_version)")
    seed(url)
    assert q(url, "SELECT (SELECT count(*) FROM core.policy), (SELECT count(*) FROM core.policy_member), (SELECT count(*) FROM config.config_version)") == before


def test_seed_integrity(seeded):
    url, _ = seeded
    assert q(url, "SELECT count(*) FROM core.policy p WHERE NOT EXISTS (SELECT 1 FROM core.policy_member m WHERE m.policy_id = p.id)")[0][0] == 0
    assert q(url, "SELECT count(*) - count(DISTINCT hmac_key_id) FROM core.network_hospital")[0][0] == 0
    st = dict(q(url, "SELECT network_status, count(*) FROM core.network_hospital GROUP BY 1"))
    assert st == {"network": 8, "non_network": 3, "blacklisted": 1}
    assert q(url, "SELECT count(*) FROM core.policy_member WHERE member_id = 'MEM-77120345' AND full_name = 'Asha Verma'")[0][0] == 1
    domains = {r[0] for r in q(url, "SELECT DISTINCT domain FROM config.config_set")}
    assert {"thresholds", "query_policy", "doc_requirements", "policy_rules"} <= domains


def test_build_master_is_deterministic():
    assert build_master() == build_master()


@pytest.fixture
async def sm(seeded):
    url, _ = seeded
    eng = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


async def test_resolve_defaults_scope_fallback_and_history(sm):
    svc = ConfigService()
    async with sm() as s:
        cfg = await svc.resolve(s, "thresholds", "default", datetime(2026, 10, 6, tzinfo=UTC))
        assert cfg.version == 1 and str(cfg.payload.t_auto_inr) == "50000"
        pr = await svc.resolve(s, "policy_rules", "HEALTH-PLUS-GOLD")
        assert pr.payload.product_code == "HEALTH-PLUS-GOLD"
        fb = await svc.resolve(s, "policy_rules", "UNKNOWN-PRODUCT")  # falls back to default scope
        assert fb.name == "default"
        with pytest.raises(ConfigNotFound):
            await svc.resolve(s, "thresholds", "default", datetime(2025, 1, 1, tzinfo=UTC))  # C5 before first version
        with pytest.raises(ConfigNotFound):
            await svc.resolve(s, "no_such_domain", "default")


async def test_two_person_rule_and_publish_window(sm):
    from claim_contract.errors import ProblemError

    svc = ConfigService()
    async with sm() as s, s.begin():
        d = await svc.create_draft(s, "thresholds", "default", {"t_auto_inr": "60000", "t_four_inr": "600000"}, "admin1", "raise limits")
    assert d["version"] == 2
    async with sm() as s:
        with pytest.raises(ProblemError, match="second_approver_required"):
            await svc.publish(s, "thresholds", "default", 2, "admin1")
        with pytest.raises(ProblemError, match="second_approver_required"):
            await svc.publish(s, "thresholds", "default", 2, "admin1", second_approver="admin1")
        await s.rollback()
    async with sm() as s, s.begin():
        await svc.publish(s, "thresholds", "default", 2, "admin1", second_approver="senior1", effective_from=datetime(2026, 12, 1, tzinfo=UTC))
    async with sm() as s:
        now_cfg = await svc.resolve(s, "thresholds", "default", datetime(2026, 11, 1, tzinfo=UTC))
        future = await svc.resolve(s, "thresholds", "default", datetime(2026, 12, 2, tzinfo=UTC))
    assert now_cfg.version == 1 and future.version == 2 and str(future.payload.t_auto_inr) == "60000"


async def test_invalid_payload_rejected_and_t_auto_lt_t_four(sm):
    from claim_contract.errors import ProblemError

    svc = ConfigService()
    async with sm() as s:
        with pytest.raises(ProblemError):
            await svc.create_draft(s, "thresholds", "default", {"t_auto_inr": "700000", "t_four_inr": "600000"}, "a", "bad")  # C10
        with pytest.raises(ProblemError):
            await svc.create_draft(s, "thresholds", "default", {"unknown": 1}, "a", "bad")


def test_name_normalisation_and_similarity():
    assert normalise_name("Dr. Ravi  Kumar S.") == "ravi kumar s"
    assert normalise_name("Åsha  Vérma") == "asha verma"
    assert name_similarity("Ravi Kumar S", "Ravi Kumar Sharma") >= 0.95
    assert name_similarity("Asha Verma", "Verma Asha") >= 0.95
    assert name_similarity("Asha Verma", "Rohit Singh") < 0.7
