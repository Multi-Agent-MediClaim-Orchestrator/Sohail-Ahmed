"""SSE hub: ticket auth, replay via Last-Event-ID, per-event authorisation (real Redis, in-process generator)."""

import asyncio
import uuid

import pytest
import redis.asyncio as aioredis
from app.auth.principal import Principal
from app.core.config import Settings
from app.sse.hub import STREAM, RedisHub, stream_events


def _user(*roles):
    return Principal("human", "s", frozenset(roles), id=uuid.uuid4())


@pytest.fixture
async def hub():
    s = Settings.from_env()
    r = aioredis.from_url(s.redis_url, decode_responses=True)
    await r.delete(STREAM)
    yield RedisHub(r, None, 100)
    await r.delete(STREAM)
    await r.aclose()


async def _collect(gen, n, wait=5):
    out = []

    async def run():
        async for f in gen:
            if f.startswith(":"):
                continue
            out.append(f)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), wait)
    return out


async def test_replay_and_live(hub):
    await hub.publish("system.a", None, {"n": 1})
    first = (await hub.history("0-0"))[0]["id"]
    await hub.publish("system.b", None, {"n": 2})
    kill = asyncio.Event()
    gen = stream_events(hub, _user("admin"), "inbox", None, first, 0.2, kill)
    task = asyncio.create_task(_collect(gen, 2))
    await asyncio.sleep(0.5)
    await hub.publish("system.c", None, {"n": 3})
    frames = await task
    kill.set()
    assert "event: system.b" in frames[0] and "event: system.c" in frames[1]


async def test_admin_sees_no_case_events(hub):
    class H(RedisHub):
        async def _case_assignee(self, cid):
            return None, True

    h = H(hub.r, None, 100)
    await h.publish("case.created", str(uuid.uuid4()), {})
    await h.publish("system.x", None, {})
    kill = asyncio.Event()
    frames = await _collect(stream_events(h, _user("admin"), "inbox", None, "0-0", 0.2, kill), 1)
    kill.set()
    assert "system.x" in frames[0]
    desk = await _collect(
        stream_events(h, _user("desk"), "inbox", None, "0-0", 0.2, asyncio.Event()), 2
    )
    assert "case.created" in desk[0]


@pytest.fixture
async def sapp(settings):
    """App with the real Redis hub (the default test app uses an in-memory one)."""
    from app.main import create_checked_app
    from app.services.n8n import RecordingN8n

    a = create_checked_app(settings, n8n=RecordingN8n())
    async with a.router.lifespan_context(a):
        yield a


async def test_ticket_endpoint_and_one_time_use(sapp, tok):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sapp), base_url="http://h") as c:
        r = await c.post("/v1/stream/ticket", json={"scope": "inbox"}, headers=tok("desk1"))
        assert r.status_code == 200 and r.json()["ticket"].startswith("tk_")
        assert (
            await c.post("/v1/stream/ticket", json={"scope": "case"}, headers=tok("desk1"))
        ).status_code == 422
        assert (await c.post("/v1/stream/ticket", json={"scope": "inbox"})).status_code == 401
        bad = await c.get("/v1/stream?ticket=nope")
        assert bad.status_code == 401
        ghost = await c.post(
            "/v1/stream/ticket",
            json={"scope": "case", "case_id": str(uuid.uuid4())},
            headers=tok("desk1"),
        )
        assert ghost.status_code == 403
        # the ticket is consumed on first use
        t = r.json()["ticket"]
        assert await sapp.state.redis.getdel("sse:hospital:ticket:" + t)
        assert (await c.get(f"/v1/stream?ticket={t}")).status_code == 401
