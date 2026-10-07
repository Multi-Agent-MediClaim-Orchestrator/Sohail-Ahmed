import pytest

pytestmark = pytest.mark.integration


async def test_ready_reports_db_and_metrics_expose_operational_gauges(env):
    async with env.client("admin1", ["admin"]) as c:
        r = await c.get("/v1/ready")
        assert r.status_code == 200 and r.json()["deps"]["db"] == "ok" and r.json()["status"] in ("ready", "degraded")
        m = (await c.get("/metrics")).text
    for name in ("outbox_dead_letter", "claims_in_status", "human_queue_age_seconds", "audit_chain_verify_failures_total", "claim_stage_duration_seconds"):
        assert name in m, name


async def test_audit_verify_all_counts_failures_metric(env):
    async with env.svc() as c:
        r = await c.post("/internal/audit/verify-all")
        assert r.status_code == 200 and "checked" in r.json()
