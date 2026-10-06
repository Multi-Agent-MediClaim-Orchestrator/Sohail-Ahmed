"""Integration smoke tests for Step 2 infra. Requires `make up-infra`. Run: make test-infra"""

import base64
import io
import json
import os
import pathlib
import socket
import struct
import subprocess

import httpx
import pytest
from minio import Minio
from minio.error import S3Error

pytestmark = pytest.mark.integration

ENV: dict[str, str] = {}
for ln in pathlib.Path(".env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        ENV[k] = v
ENV.update({k: v for k, v in os.environ.items() if k in ENV})
MINIO = f"localhost:{ENV.get('SHARED_MINIO_PORT', '9000')}"
KC = "http://localhost:8080"
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def redis(user: str, pw: str, *args: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "exec",
            "claims-redis-1",
            "redis-cli",
            "--user",
            user,
            "--pass",
            pw,
            "--no-auth-warning",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # noqa: S603
    return (r.stdout + r.stderr).strip()


def test_redis_acl_isolation() -> None:
    hosp, ins = ENV["HOSP_REDIS_PW"], ENV["INS_REDIS_PW"]
    assert redis("hosp_app", hosp, "SET", "idem:hosp:k1:a", "1", "EX", "60") == "OK"
    assert "NOPERM" in redis("hosp_app", hosp, "GET", "idem:ins:k1:a")
    assert "NOPERM" in redis("ins_app", ins, "GET", "idem:hosp:k1:a")
    assert "NOPERM" in redis("hosp_app", hosp, "FLUSHALL")
    assert "NOPERM" in redis("hosp_app", hosp, "KEYS", "*")
    assert "WRONGPASS" in redis("default", "x", "PING") or "invalid" in redis(
        "default", "x", "PING"
    )


def mc(user: str, env: str) -> Minio:
    return Minio(MINIO, user, ENV[env], secure=False)


def test_minio_isolation_and_sse() -> None:
    hosp = mc("hosp_svc", "HOSP_MINIO_SECRET")
    hosp.put_object("hospital-docs", "case1/doc1/original.pdf", io.BytesIO(b"x" * 1024), 1024)
    st = hosp.stat_object("hospital-docs", "case1/doc1/original.pdf")
    assert st.version_id  # versioning on
    assert (st.metadata or {}).get("x-amz-server-side-encryption", "").upper() in ("AES256", "")
    ins = mc("ins_svc", "INS_MINIO_SECRET")
    with pytest.raises(S3Error) as ei:
        ins.get_object("hospital-docs", "case1/doc1/original.pdf")
    assert ei.value.code == "AccessDenied"
    with pytest.raises(S3Error):
        ins.put_object("hospital-docs", "evil", io.BytesIO(b"x"), 1)
    ins.put_object("insurer-docs", "IC-1/d/original.pdf", io.BytesIO(b"y"), 1)  # own bucket ok
    with pytest.raises(S3Error):
        hosp.get_object("insurer-docs", "IC-1/d/original.pdf")


def test_minio_docpipe_write_limited_to_derived() -> None:
    dp = mc("docpipe_hosp", "DOCPIPE_HOSP_MINIO_SECRET")
    dp.put_object("hospital-docs", "case1/doc1/parsed.json", io.BytesIO(b"{}"), 2)
    with pytest.raises(S3Error):
        dp.put_object("hospital-docs", "case1/doc1/original.pdf", io.BytesIO(b"x"), 1)


def test_presigned_url_download() -> None:
    url = mc("hosp_svc", "HOSP_MINIO_SECRET").presigned_get_object(
        "hospital-docs", "case1/doc1/original.pdf"
    )
    assert httpx.get(url).content == b"x" * 1024


def clam(data: bytes) -> str:
    with socket.create_connection(("localhost", 3310), timeout=10) as s:
        s.sendall(b"zINSTREAM\0" + struct.pack(">I", len(data)) + data + struct.pack(">I", 0))
        return s.recv(512).decode().strip("\0\n ")


def test_clamav() -> None:
    assert "FOUND" in clam(EICAR)
    assert clam(b"%PDF-1.4 clean").endswith("OK")


def token(realm: str, client: str, secret: str) -> dict:  # type: ignore[type-arg]
    r = httpx.post(
        f"{KC}/realms/{realm}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": client, "client_secret": secret},
    )
    assert r.status_code == 200, r.text
    payload = r.json()["access_token"].split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))  # type: ignore[no-any-return]


def test_keycloak_claims_and_realm_separation() -> None:
    h = token("hospital", "hospital-n8n", ENV["HOSP_N8N_CLIENT_SECRET"])
    assert h["iss"].endswith("/realms/hospital") and h["system"] == "hospital"
    assert "svc-n8n" in h["realm_access"]["roles"]
    aud = h["aud"] if isinstance(h["aud"], list) else [h["aud"]]
    assert "hospital-api" in aud
    i = token("insurer", "insurer-crew", ENV["INS_CREW_CLIENT_SECRET"])
    assert i["iss"].endswith("/realms/insurer") and i["system"] == "insurer"
    assert i["iss"] != h["iss"]  # an API validating `iss` rejects the other realm's tokens


def test_keycloak_discovery_and_jwks_differ() -> None:
    keys = []
    for realm in ("hospital", "insurer"):
        assert httpx.get(f"{KC}/realms/{realm}/.well-known/openid-configuration").status_code == 200
        keys.append(
            httpx.get(f"{KC}/realms/{realm}/protocol/openid-connect/certs").json()["keys"][0]["kid"]
        )
    assert keys[0] != keys[1]


# ---- gaps closed in phase 1: pub/sub channel ACLs, contract RedisStore on real ACLs, lifecycle ----
async def test_redis_pubsub_channel_restrictions() -> None:
    import redis.asyncio as aioredis

    hosp = aioredis.Redis(
        port=int(ENV["SHARED_REDIS_PORT"]),
        username="hosp_app",
        password=ENV["HOSP_REDIS_PW"],
        decode_responses=True,
    )
    ins = aioredis.Redis(
        port=int(ENV["SHARED_REDIS_PORT"]),
        username="ins_app",
        password=ENV["INS_REDIS_PW"],
        decode_responses=True,
    )
    try:
        ps = hosp.pubsub()
        await ps.subscribe("sse:hospital:case1", "cfg:changed", "audit:appended")  # allowed
        await ps.unsubscribe()
        assert await hosp.publish("sse:hospital:case1", "x") >= 0
        for forbidden in ("sse:insurer:case1", "other:chan"):
            with pytest.raises(aioredis.ResponseError, match="No permissions"):
                await hosp.publish(forbidden, "x")
        with pytest.raises(aioredis.ResponseError, match="No permissions"):
            await ins.publish("sse:hospital:case1", "x")
        assert await ins.publish("sse:insurer:case1", "x") >= 0
    finally:
        await hosp.aclose()
        await ins.aclose()


async def test_contract_redis_store_works_under_acl() -> None:
    import redis.asyncio as aioredis
    from claim_contract.idempotency import RedisStore, StoredResponse

    r = aioredis.Redis(
        port=int(ENV["SHARED_REDIS_PORT"]), username="hosp_app", password=ENV["HOSP_REDIS_PW"]
    )
    try:
        s = RedisStore(r, "hosp")
        key = "hosp-001:" + os.urandom(4).hex()
        assert await s.get(key) is None
        assert await s.acquire_lock(key) and not await s.acquire_lock(key)
        await s.put(
            key, StoredResponse("h", 202, b'{"ok":1}', {"content-type": "application/json"})
        )
        got = await s.get(key)
        assert got and got.status == 202 and got.body == b'{"ok":1}'
        await s.release_lock(key)
        assert await s.acquire_lock(key)
        await s.release_lock(key)
    finally:
        await r.aclose()


def test_minio_lifecycle_rule_present() -> None:
    admin = Minio(
        MINIO, ENV["SHARED_MINIO_ROOT_USER"], ENV["SHARED_MINIO_ROOT_PASSWORD"], secure=False
    )
    for bucket in ("hospital-docs", "insurer-docs"):
        rules = admin.get_bucket_lifecycle(bucket).rules
        assert any(r.noncurrent_version_expiration.noncurrent_days == 30 for r in rules)
    assert admin.get_bucket_versioning("hospital-docs").status == "Enabled"
    assert admin.get_object_lock_config("audit-anchors").mode == "COMPLIANCE"
