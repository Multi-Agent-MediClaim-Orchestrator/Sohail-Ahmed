import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from builders import line, make_input
from calc_engine.api import create_app

GOLD = Path(__file__).parents[1] / "golden"


def token(sub="svc-insurer-api", secret="s3cret", aud="calc-engine"):
    return jwt.encode({"sub": sub, "aud": aud, "exp": int(time.time()) + 60}, secret, algorithm="HS256")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CALC_ENGINE_JWT_SECRET", "s3cret")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://calc")


def H(t=None):
    return {"Authorization": f"Bearer {t or token()}"}


async def test_calculate_ok_and_version_health(client):
    inp = json.loads((GOLD / "ex01.json").read_text(encoding="utf-8"))["input"]
    r = await client.post("/v1/calculate", json=inp, headers=H())
    assert r.status_code == 200 and r.json()["payable_total"] == "98000.00"
    assert (await client.get("/v1/health")).status_code == 200
    v = (await client.get("/v1/version")).json()
    assert v["rules_schema_versions"] == [1] and "room_first" in v["order_profiles"]


async def test_auth_required_and_wrong_subject(client):
    inp = make_input([line("L1", "medicine", "1.00")])
    assert (await client.post("/v1/calculate", json=inp)).status_code == 401
    assert (await client.post("/v1/calculate", json=inp, headers=H(token(secret="bad")))).status_code == 401
    assert (await client.post("/v1/calculate", json=inp, headers=H(token(sub="evil")))).status_code == 403


async def test_validation_errors(client):
    inp = make_input([line("L1", "medicine", "1.00")])
    inp["lines"][0]["claimed_amount"] = 1.0
    r = await client.post("/v1/calculate", json=inp, headers=H())
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    inp = make_input([line("L1", "medicine", "1.00")])
    del inp["rules"]["room_rent"]
    r = await client.post("/v1/calculate", json=inp, headers=H())
    assert r.status_code == 422 and r.json()["code"] == "rules_invalid"


async def test_too_many_lines_413(client, monkeypatch):
    monkeypatch.setattr("calc_engine.api.MAX_LINES", 1)
    inp = make_input([line("L1", "medicine", "1.00"), line("L2", "medicine", "1.00")])
    r = await client.post("/v1/calculate", json=inp, headers=H())
    assert r.status_code == 413


async def test_batch_with_one_invalid_item_and_explain(client):
    good = json.loads((GOLD / "ex06.json").read_text(encoding="utf-8"))["input"]
    bad = make_input([line("L1", "medicine", "1.00")])
    bad["lines"] = []
    r = await client.post("/v1/calculate/batch", json={"inputs": [good, bad]}, headers=H())
    res = r.json()["results"]
    assert res[0]["payable_total"] == "50000.00" and res[1]["code"] == "validation_error"
    e = await client.post("/v1/calculate/explain", json=good, headers=H())
    assert e.status_code == 200 and "PAYABLE 50,000.00" in e.text
    r = await client.post("/v1/calculate/batch", json={"inputs": [good] * 501}, headers=H())
    assert r.status_code == 413


async def test_invariant_failure_is_500_with_trace(client, monkeypatch):
    from calc_engine import api
    from calc_engine.errors import EngineInvariantError

    def boom(_):
        raise EngineInvariantError("forced", [{"step": "S0"}])

    monkeypatch.setattr(api, "run", boom)
    r = await client.post("/v1/calculate", json=make_input([line("L1", "medicine", "1.00")]), headers=H())
    assert r.status_code == 500 and r.json()["code"] == "engine_invariant_violated" and "S0" in r.json()["detail"]
