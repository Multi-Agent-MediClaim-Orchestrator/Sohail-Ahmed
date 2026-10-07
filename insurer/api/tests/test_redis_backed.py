"""insurer-api against a REAL Redis (docker): hot-path idempotency, rate limiting, config cache, event bus, and graceful degradation."""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aredis
from claim_contract.samples import make_submission
from ins_helpers import register_claim_docs, unique_member

pytestmark = pytest.mark.integration


@pytest.fixture
async def rcl(redis_url):
    c = aredis.Redis.from_url(redis_url, decode_responses=True)
    await c.flushall()
    yield c
    await c.aclose()


async def swap_in_redis(env, client):
    from app.main import create_app
    from app.services import events
    from app.settings import get_settings
    from hospital_sim import HospitalSim

    app = create_app(get_settings(), redis=client)
    app.state.redis = client
    events.set_bus(events.RedisEventBus(client, get_settings().events_stream, 1000))
    env.app = app
    env.sim = HospitalSim(app)
    return app


def claim():
    return make_submission(claim_ref=f"HC-2026-{uuid.uuid4().int % 900000 + 100000}", doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **unique_member())


async def test_idempotency_rate_limit_and_events_use_redis(env, rcl):
    from app.services import jobs

    await swap_in_redis(env, rcl)
    c = claim()
    register_claim_docs(env, c)
    idem = str(uuid.uuid4())
    r1 = await env.sim.submit(c, idem=idem)
    r2 = await env.sim.submit(c, idem=idem)
    assert r1.status_code == 202 and r2.status_code in (200, 202) and r2.headers.get("Idempotent-Replay") == "true"
    assert r1.json()["insurer_claim_no"] == r2.json()["insurer_claim_no"]
    keys = await rcl.keys("*")
    assert any("idem" in k for k in keys), keys  # hot path really lives in Redis
    assert any("rl" in k or "rate" in k for k in keys), keys
    await jobs.drain({"fetch_documents", "start_verification"})
    assert await rcl.xlen(env.settings.events_stream) >= 1  # SSE bus is a Redis stream


async def test_degrades_when_redis_goes_away(env, rcl):
    await swap_in_redis(env, rcl)
    await rcl.aclose()  # every later call raises; the API must keep serving from SQL / memory
    bad = aredis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    await swap_in_redis(env, bad)
    c = claim()
    register_claim_docs(env, c)
    idem = str(uuid.uuid4())
    r = await env.sim.submit(c, idem=idem)
    assert r.status_code == 202, r.text
    again = await env.sim.submit(c, idem=idem)
    assert again.status_code in (200, 202) and again.json()["insurer_claim_no"] == r.json()["insurer_claim_no"]  # durable SQL idempotency
