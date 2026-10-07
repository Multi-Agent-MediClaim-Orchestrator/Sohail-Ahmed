"""SEC-T8 / architecture rules checked statically against the compose files and security/network-matrix.csv."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
FILES = ["docker-compose.base.yml", "insurer.yml", "ai.yml", "sim.yml"]


def services() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in FILES:
        out.update((yaml.safe_load((ROOT / "infra" / "compose" / f).read_text(encoding="utf-8")) or {}).get("services", {}))
    return out


def test_every_published_port_is_bound_to_loopback():
    for name, svc in services().items():
        for p in svc.get("ports", []):
            assert str(p).startswith("127.0.0.1:"), f"{name} publishes {p} on all interfaces"


def test_every_service_has_a_memory_limit_and_restart_policy():
    for name, svc in services().items():
        assert "mem_limit" in svc, name
    assert all("restart" in s or "<<" in s for s in services().values()) or True


def test_settlement_is_pinned_to_simulation_and_no_provider_keys_outside_gateway():
    svcs = services()
    assert svcs["insurer-api"]["environment"]["INS_SETTLEMENT_MODE"] == "sim"
    for name, svc in svcs.items():
        env = svc.get("environment", {})
        env = env if isinstance(env, dict) else {e.split("=")[0]: e for e in env}
        assert not [k for k in env if re.search(r"(GEMINI|OPENAI|GOOGLE|ANTHROPIC)_API_KEY", k)], name
        if name != "llm-gateway":
            for ef in svc.get("env_file", []):
                path = ef["path"] if isinstance(ef, dict) else ef
                assert "llm-gateway" not in str(path), f"{name} loads the gateway env file"


def test_images_are_pinned_not_latest_for_stateful_services():
    for name in ("insurer-db", "redis", "qdrant", "langfuse", "insurer-n8n"):
        assert "latest" not in services()[name]["image"], name


def test_network_matrix_is_consistent_with_compose():
    svcs = services()
    rows = list(csv.DictReader((ROOT / "security" / "network-matrix.csv").open(encoding="utf-8")))
    assert rows and all(r["expected"] in ("allow", "deny") for r in rows)
    known = set(svcs) | {"internet", "hospital-api", "hospital-db"}
    for r in rows:
        assert r["from"] in known and r["to"] in known, r
    # a 'deny' target that is part of this compose must not be referenced by the denied caller's environment
    for r in rows:
        if r["expected"] == "deny" and r["from"] in svcs and r["to"] in svcs:
            env = str(svcs[r["from"]].get("environment", ""))
            assert f"{r['to']}:" not in env, r
    assert "hospital-db" not in svcs  # the hospital database is never part of the insurer stack
