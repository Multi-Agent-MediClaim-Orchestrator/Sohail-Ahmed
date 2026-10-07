from __future__ import annotations

import asyncio
import importlib.util
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest
import yaml

GW = Path(__file__).resolve().parents[1]
ROOT = GW.parents[1]
sys.path.insert(0, str(GW))

from callbacks import core  # noqa: E402
from callbacks.core import (  # noqa: E402
    GatewayError,
    TokenBudget,
    check_metadata,
    check_model_allowed,
    pii_guard,
    pii_hits,
)

CFG = yaml.safe_load((GW / "config.yaml").read_text(encoding="utf-8"))
LOCAL = yaml.safe_load((GW / "config.local.yaml").read_text(encoding="utf-8"))
META = {"system": "insurer", "agent": "coverage", "prompt_version": "coverage@3", "claim_ref": "HC-2026-000123"}


def req(model="reason-cloud", text="hello", **kw):
    return {"model": model, "messages": [{"role": "user", "content": text}], "metadata": dict(META), **kw}


# ---------------------------------------------------------------- config (T1, T17, T19)
def test_all_aliases_defined_and_fallbacks_resolve():
    names = {m["model_name"] for m in CFG["model_list"]}
    assert {"cleanup-local", "cleanup-cloud", "reason-cloud", "reason-local", "vision-cloud", "embed"} <= names
    for chain in CFG["router_settings"]["fallbacks"] + CFG["router_settings"]["context_window_fallbacks"]:
        for src, dsts in chain.items():
            assert src in names and set(dsts) <= names


def test_embed_has_no_fallback_to_other_dimension():
    for chain in CFG["router_settings"]["fallbacks"]:
        assert "embed" not in chain


def test_only_cloud_aliases_use_provider_keys():
    for m in CFG["model_list"]:
        params = m["litellm_params"]
        if "api_key" in params:
            assert "cloud" in m["model_name"]
            assert params["api_key"] == "os.environ/GEMINI_API_KEY"  # never a literal
        else:
            assert params["model"].startswith("ollama/")


def test_no_literal_secrets_in_config_files():
    for f in (GW / "config.yaml", GW / "config.local.yaml", GW / ".env.example"):
        txt = f.read_text(encoding="utf-8")
        assert not re.search(r"AIza[0-9A-Za-z_-]{35}", txt)
        assert not re.search(r"sk-[A-Za-z0-9]{32,}", txt)
    assert re.search(r"^GEMINI_API_KEY=\s*(#.*)?$", (GW / ".env.example").read_text(encoding="utf-8"), re.M)


def test_local_overlay_has_no_outbound_provider():
    for m in LOCAL["model_list"]:
        assert m["litellm_params"]["model"].startswith("ollama/"), m["model_name"]
        assert "api_key" not in m["litellm_params"]
    assert "vision-cloud" not in {m["model_name"] for m in LOCAL["model_list"]}
    for chain in LOCAL["router_settings"]["fallbacks"] + LOCAL["router_settings"]["context_window_fallbacks"]:
        for dsts in chain.values():
            assert not any(d.endswith("-cloud") for d in dsts)
    # every alias the cloud config serves except vision is still served
    assert {m["model_name"] for m in CFG["model_list"]} - {m["model_name"] for m in LOCAL["model_list"]} == {"vision-cloud"}


def test_callbacks_modules_exist_and_are_wired():
    for dotted in CFG["litellm_settings"]["callbacks"]:
        mod, attr = dotted.rsplit(".", 1)
        spec = importlib.util.find_spec(f"{mod}")
        assert spec is not None, mod
        assert hasattr(importlib.import_module(mod), attr)


import importlib  # noqa: E402


# ---------------------------------------------------------------- PII guard (T4-T8)
@pytest.mark.parametrize("text,name", [
    ("Aadhaar 2345 6789 0123 given", "aadhaar"), ("id 234567890123", "aadhaar"), ("PAN ABCDE1234F", "pan"),
    ("call +91 98765 43210".replace(" 98765 43210", "9876543210"), "mobile"), ("mail a.b@example.com", "email"), ("member MEM-12345678", "member_id_raw"),
])
def test_pii_blocked_on_cloud_with_no_text_in_error(text, name):
    with pytest.raises(GatewayError) as e:
        pii_guard(req(text=text), "insurer-crew", mode="enforce")
    assert e.value.status == 400 and e.value.code == "pii_detected" and name in e.value.message
    assert not re.search(r"\d{6,}", e.value.message) and "ABCDE" not in e.value.message and "@" not in e.value.message


def test_same_text_allowed_to_local():
    pii_guard(req(model="reason-local", text="Aadhaar 2345 6789 0123 PAN ABCDE1234F"), "insurer-crew", mode="enforce")


def test_presidio_placeholders_allowed():
    pii_guard(req(text="Patient <PERSON_1> phone <PHONE_2> id <ID_3> mail <EMAIL_1>"), "insurer-crew", mode="enforce")


def test_currency_prefixed_digits_are_amounts():
    pii_guard(req(text="Total INR 123456789012 and Rs. 234567890123 and ₹234567890123"), "insurer-crew", mode="enforce")
    assert pii_hits("amount: 987654321098") == {"aadhaar"}  # without a currency marker the same shape is suspicious


def test_log_mode_does_not_block_but_audits():
    seen = []
    pii_guard(req(text="PAN ABCDE1234F"), "insurer-crew", mode="log", audit=lambda ev, **kw: seen.append((ev, kw)))
    assert seen and seen[0][1]["hits"] == ["pan"] and "ABCDE" not in str(seen)


def test_image_bytes_ignored_but_text_checked():
    body = req(messages=None)
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": "stamp region"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "2345" * 50}}]}]
    pii_guard(body, "vision-service", mode="enforce")
    body["messages"][0]["content"][0]["text"] = "PAN ABCDE1234F"
    with pytest.raises(GatewayError):
        pii_guard(body, "vision-service", mode="enforce")


def test_embedding_input_is_scanned_on_cloud_only():
    pii_guard({"model": "embed", "input": ["PAN ABCDE1234F"], "metadata": {"system": "shared", "agent": "rag", "prompt_version": "n/a"}}, "rag-service", mode="enforce")


# ---------------------------------------------------------------- metadata + allow-list (T11, T13)
@pytest.mark.parametrize("mut", [lambda m: m.pop("system"), lambda m: m.update(system="other"), lambda m: m.pop("agent"), lambda m: m.pop("prompt_version")])
def test_metadata_required(mut):
    d = req()
    mut(d["metadata"])
    with pytest.raises(GatewayError) as e:
        check_metadata(d, "insurer-crew")
    assert e.value.status == 400 and e.value.code == "metadata_required"


def test_eval_and_n8n_keys_need_only_system_and_missing_claim_ref_gets_kb_session():
    d = {"model": "reason-local", "messages": [], "metadata": {"system": "shared"}}
    assert check_metadata(d, "n8n-insurer")["metadata"]["session_id"] == "kb-ingest"
    with pytest.raises(GatewayError):
        check_metadata({"model": "x", "metadata": {"system": "shared"}}, "insurer-crew")


def test_stream_with_json_rejected():
    with pytest.raises(GatewayError) as e:
        check_metadata(req(stream=True, response_format={"type": "json_object"}), "insurer-crew")
    assert e.value.status == 400


def test_virtual_key_allow_list():
    check_model_allowed("reason-cloud", "insurer-crew")
    for alias in ("cleanup-local", "vision-cloud", "embed", "cleanup-cloud"):
        with pytest.raises(GatewayError) as e:
            check_model_allowed(alias, "insurer-crew")
        assert e.value.status == 403 and e.value.code == "model_not_allowed"
    check_model_allowed("anything", "eval-harness")
    check_model_allowed("anything", None)  # master key / unknown caller is not restricted here


def test_virtual_key_table_matches_spec():
    assert core.VIRTUAL_KEYS["insurer-crew"] == {"models": ["reason-cloud", "reason-local"], "rpm": 30, "budget": 600_000}
    assert core.VIRTUAL_KEYS["vision-service"]["models"] == ["vision-cloud"]
    assert len(core.VIRTUAL_KEYS) == 9


# ---------------------------------------------------------------- token budget (T12)
def test_token_budget_enforced_and_resets_next_utc_day():
    kv = fakeredis.FakeAsyncRedis(decode_responses=True)
    tb = TokenBudget(kv, budgets={"insurer-crew": 1000})
    day1 = datetime(2026, 10, 7, 23, 59, tzinfo=UTC)

    async def go():
        await tb.check("insurer-crew", "reason-cloud", day1)
        await tb.record("insurer-crew", "reason-cloud", 1000, day1)
        with pytest.raises(GatewayError) as e:
            await tb.check("insurer-crew", "reason-cloud", day1)
        assert e.value.status == 429 and e.value.code == "budget_exceeded"
        await tb.check("insurer-crew", "reason-local", day1)  # local aliases are free
        await tb.record("insurer-crew", "reason-local", 99999, day1)
        await tb.check("insurer-crew", "reason-cloud", day1 + timedelta(minutes=2))  # next UTC day

    asyncio.run(go())


def test_token_budget_fails_open_when_redis_down():
    class Broken:
        async def get(self, k):
            raise ConnectionError

        async def incrby(self, k, n):
            raise ConnectionError

        async def expire(self, k, s):
            raise ConnectionError

    tb = TokenBudget(Broken())
    asyncio.run(tb.check("insurer-crew", "reason-cloud"))
    asyncio.run(tb.record("insurer-crew", "reason-cloud", 10))
    strict = TokenBudget(Broken(), fail_open=False)
    with pytest.raises(ConnectionError):
        asyncio.run(strict.check("insurer-crew", "reason-cloud"))


def test_fallback_tags():
    assert core.fallback_tags("reason-cloud", "reason-cloud") == {"x-llm-fallback-used": "false"}
    t = core.fallback_tags("reason-cloud", "reason-local")
    assert t["x-llm-fallback-used"] == "true" and t["langfuse_tag"] == "fallback:reason-cloud->reason-local"


# ---------------------------------------------------------------- scripts
def test_cost_by_claim_aggregate():
    spec = importlib.util.spec_from_file_location("cbc", GW / "scripts" / "cost_by_claim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    gens = [
        {"sessionId": "HC-1", "name": "coverage", "latency": 1.0, "usage": {"totalTokens": 100}, "tags": []},
        {"sessionId": "HC-1", "name": "coverage", "latency": 3.0, "usage": {"totalTokens": 50}, "tags": ["fallback:a->b"]},
        {"sessionId": "HC-2", "name": "query", "latency": 2.0, "usage": {"totalTokens": 7}},
    ]
    rows = mod.aggregate(gens)
    assert rows[0] == {"claim_ref": "HC-1", "agent": "coverage", "calls": 2, "tokens": 150, "p50_s": 2.0, "p95_s": 3.0, "fallbacks": 1}
    assert "| HC-2 | query | 1 | 7 |" in mod.to_markdown(rows)


def test_init_keys_payload_matches_table():
    spec = importlib.util.spec_from_file_location("ik", GW / "scripts" / "init_keys.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.payload("insurer-crew", core.VIRTUAL_KEYS["insurer-crew"])
    assert p["models"] == ["reason-cloud", "reason-local"] and p["rpm_limit"] == 30 and p["budget_duration"] == "1d"
    assert mod.env_name("insurer-crew") == "INSURER_CREW_KEY"


@pytest.mark.skipif(subprocess.run(["bash", "-c", "true"], capture_output=True).returncode != 0, reason="bash unavailable")
def test_ci_provider_key_grep(tmp_path):
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "ci_no_provider_keys.sh").write_bytes((ROOT / "scripts" / "ci_no_provider_keys.sh").read_bytes())
    tree = tmp_path / "tree"
    (tree / "svc").mkdir(parents=True)

    def run() -> int:
        return subprocess.run(["bash", "../tool/ci_no_provider_keys.sh", "."], cwd=tree, capture_output=True).returncode

    (tree / "svc" / "compose.yml").write_text("env:\n  - TZ=UTC\n", encoding="utf-8")
    assert run() == 0
    (tree / "llm-gateway").mkdir()
    (tree / "llm-gateway" / ".env").write_text("GEMINI_API_KEY=x\n", encoding="utf-8")  # allowed there
    assert run() == 0
    (tree / "svc" / "compose.yml").write_text("env:\n  - GEMINI_API_KEY=abc\n", encoding="utf-8")
    assert run() == 1
    (tree / "svc" / "compose.yml").write_text("k: AIza" + "A" * 35 + "\n", encoding="utf-8")
    assert run() == 1
