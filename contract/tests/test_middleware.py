import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import httpx
import pytest
from claim_contract import signing
from claim_contract.idempotency import RedisStore, StoredResponse
from claim_contract.middleware import (
    ContractConfig,
    ContractMiddleware,
    RateLimiter,
    journey_id_var,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SECRET = b"test-secret-hosp-to-ins-0001"
KEY = "hosp-001"


def make_app(calls: list[str], cfg: ContractConfig) -> Starlette:
    async def claims(request: Request) -> JSONResponse:
        body = await request.body()
        calls.append(body.decode())
        if body == b'{"boom":1}' and len(calls) == 1:
            return JSONResponse({"err": 1}, status_code=500)
        await asyncio.sleep(0.05)
        return JSONResponse({"n": len(calls), "journey": journey_id_var.get()}, status_code=202)

    async def get_claim(request: Request) -> JSONResponse:
        return JSONResponse({"ref": request.path_params["ref"], "q": request.url.query})

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/v1/hospital-api/claims", claims, methods=["POST"]),
            Route("/v1/hospital-api/claims/{ref}", get_claim),
            Route("/v1/health", health),
        ]
    )
    app.add_middleware(ContractMiddleware, config=cfg)
    return app


def signed(
    method: str,
    path: str,
    body: bytes = b"",
    idem: str | None = None,
    ts: str | None = None,
    **headers: str,
) -> tuple[str, bytes, dict[str, str]]:
    ts = ts or signing.now_ts()
    idem = idem if method != "GET" else None
    h = {
        "X-Contract-Version": "1.1",
        "X-Key-Id": KEY,
        "X-Timestamp": ts,
        "X-Signature": signing.sign(SECRET, method, path, ts, idem, body),
    }
    if idem:
        h["X-Idempotency-Key"] = idem
    h.update(headers)
    return path, body, h


async def call(
    app: Starlette, method: str, path: str, body: bytes = b"", **kw: str
) -> httpx.Response:
    p, b, h = signed(method, path, body, **kw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, p, content=b, headers=h)


@pytest.fixture
def redis():  # type: ignore[no-untyped-def]
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def cfg(redis) -> ContractConfig:  # type: ignore[no-untyped-def]
    return ContractConfig(
        secrets={KEY: SECRET},
        store=RedisStore(redis, "hosp"),
        rate_limiter=RateLimiter("hosp", limit=100, redis=redis),
    )


async def test_valid_get_and_headers(cfg: ContractConfig) -> None:
    r = await call(
        make_app([], cfg), "GET", "/v1/hospital-api/claims/HC-2026-000001?include=queries"
    )
    assert r.status_code == 200 and r.json()["q"] == "include=queries"
    assert r.headers["x-contract-version"] == "1.1" and r.headers["x-request-id"]
    assert r.headers["x-ratelimit-remaining"] == "99"


async def test_open_paths_need_no_signature(cfg: ContractConfig) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app([], cfg)), base_url="http://t"
    ) as c:
        assert (await c.get("/v1/health")).status_code == 200


async def test_tampered_body_path_key_and_missing_headers(cfg: ContractConfig) -> None:
    app = make_app([], cfg)
    idem = str(uuid.uuid4())
    p, b, h = signed("POST", "/v1/hospital-api/claims", b'{"a":1}', idem)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(p, content=b'{"a":2}', headers=h)
        assert r.status_code == 401 and r.json()["code"] == "invalid_signature"
        r = await c.post(p + "?x=1", content=b, headers=h)
        assert r.json()["code"] == "invalid_signature"
        r = await c.post(p, content=b, headers={**h, "X-Key-Id": "nope"})
        assert r.json()["code"] == "invalid_signature"
        r = await c.post(p, content=b, headers={k: v for k, v in h.items() if k != "X-Signature"})
        assert r.json()["code"] == "invalid_signature"
        assert r.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(("delta", "status"), [(301, 401), (-301, 401), (299, 202), (-299, 202)])
async def test_skew(cfg: ContractConfig, delta: int, status: int) -> None:
    ts = (datetime.now(UTC) + timedelta(seconds=delta)).strftime(signing.TS_FORMAT)
    r = await call(
        make_app([], cfg), "POST", "/v1/hospital-api/claims", b"{}", idem=str(uuid.uuid4()), ts=ts
    )
    assert r.status_code == status
    if status == 401:
        assert r.json()["code"] == "stale_request"


async def test_unsupported_major_and_big_body(cfg: ContractConfig) -> None:
    r = await call(
        make_app([], cfg), "GET", "/v1/hospital-api/claims/x", **{"X-Contract-Version": "2.0"}
    )
    assert r.status_code == 400 and r.json()["code"] == "unsupported_version"
    r = await call(
        make_app([], cfg),
        "POST",
        "/v1/hospital-api/claims",
        b"x" * (2 * 1024 * 1024 + 1),
        idem=str(uuid.uuid4()),
    )
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


async def test_idempotent_replay_conflict_and_single_effect(cfg: ContractConfig) -> None:
    calls: list[str] = []
    app, idem = make_app(calls, cfg), str(uuid.uuid4())
    r1 = await call(app, "POST", "/v1/hospital-api/claims", b'{"a":1}', idem=idem)
    r2 = await call(app, "POST", "/v1/hospital-api/claims", b'{"a":1}', idem=idem)
    assert r1.status_code == r2.status_code == 202 and len(calls) == 1
    assert r2.headers["idempotent-replay"] == "true" and "idempotent-replay" not in r1.headers
    assert r2.json() == r1.json()
    r3 = await call(app, "POST", "/v1/hospital-api/claims", b'{"a":2}', idem=idem)
    assert r3.status_code == 409 and r3.json()["code"] == "idempotency_conflict"


async def test_concurrent_same_key_one_executes(cfg: ContractConfig) -> None:
    calls: list[str] = []
    app, idem = make_app(calls, cfg), str(uuid.uuid4())
    rs = await asyncio.gather(
        *[call(app, "POST", "/v1/hospital-api/claims", b'{"c":1}', idem=idem) for _ in range(2)]
    )
    assert len(calls) == 1
    assert sorted(r.status_code for r in rs) in ([202, 202], [202, 409])
    for r in rs:
        if r.status_code == 409:
            assert r.json()["code"] == "idempotency_in_progress"


async def test_5xx_not_stored_then_retry_succeeds(cfg: ContractConfig) -> None:
    calls: list[str] = []
    app, idem = make_app(calls, cfg), str(uuid.uuid4())
    assert (
        await call(app, "POST", "/v1/hospital-api/claims", b'{"boom":1}', idem=idem)
    ).status_code == 500
    r = await call(app, "POST", "/v1/hospital-api/claims", b'{"boom":1}', idem=idem)
    assert r.status_code == 202 and len(calls) == 2


class DictDurable:
    def __init__(self) -> None:
        self.d: dict[str, StoredResponse] = {}

    async def get(self, key: str) -> StoredResponse | None:
        return self.d.get(key)

    async def put(self, key: str, rec: StoredResponse) -> None:
        self.d[key] = rec


async def test_redis_flush_falls_back_to_durable_store(redis) -> None:  # type: ignore[no-untyped-def]
    cfg = ContractConfig(
        secrets={KEY: SECRET}, store=RedisStore(redis, "hosp", durable=DictDurable())
    )
    calls: list[str] = []
    app, idem = make_app(calls, cfg), str(uuid.uuid4())
    await call(app, "POST", "/v1/hospital-api/claims", b'{"d":1}', idem=idem)
    await redis.flushall()
    r = await call(app, "POST", "/v1/hospital-api/claims", b'{"d":1}', idem=idem)
    assert len(calls) == 1 and r.headers["idempotent-replay"] == "true"


async def test_rate_limit_and_ids(redis) -> None:  # type: ignore[no-untyped-def]
    cfg = ContractConfig(
        secrets={KEY: SECRET}, rate_limiter=RateLimiter("hosp", limit=3, redis=redis)
    )
    app = make_app([], cfg)
    codes = [(await call(app, "GET", "/v1/hospital-api/claims/x")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await call(app, "GET", "/v1/hospital-api/claims/x")
    assert r.json()["code"] == "rate_limited" and "retry-after" in r.headers


async def test_journey_id_propagates_but_not_signed(cfg: ContractConfig) -> None:
    jid = str(uuid.uuid4())
    r = await call(
        make_app([], cfg),
        "POST",
        "/v1/hospital-api/claims",
        b"{}",
        idem=str(uuid.uuid4()),
        **{"X-Journey-Id": jid},
    )
    assert json.loads(r.content)["journey"] == jid


async def test_signatures_can_be_disabled_for_schema_tests() -> None:
    cfg = ContractConfig(secrets={}, verify_signatures=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app([], cfg)), base_url="http://t"
    ) as c:
        r = await c.get(
            "/v1/hospital-api/claims/x",
            headers={
                "X-Contract-Version": "1.1",
                "X-Key-Id": "k",
                "X-Timestamp": "t",
                "X-Signature": "s",
            },
        )
        assert r.status_code == 200


@pytest.mark.parametrize("idem", [None, "", "not-a-uuid"])
async def test_mutating_call_requires_uuid_idempotency_key(
    cfg: ContractConfig, idem: str | None
) -> None:
    ts = signing.now_ts()
    h = {
        "X-Contract-Version": "1.1",
        "X-Key-Id": KEY,
        "X-Timestamp": ts,
        "X-Signature": signing.sign(SECRET, "POST", "/v1/hospital-api/claims", ts, idem, b"{}"),
    }
    if idem is not None:
        h["X-Idempotency-Key"] = idem
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app([], cfg)), base_url="http://t"
    ) as c:
        r = await c.post("/v1/hospital-api/claims", content=b"{}", headers=h)
    assert r.status_code == 400 and r.json()["code"] == "bad_request"
