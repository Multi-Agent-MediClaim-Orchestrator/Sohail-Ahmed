from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

N8N = Path(__file__).resolve().parents[1]
ROOT = N8N.parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


bf = load(N8N / "build_flows.py", "build_flows")
chk = load(ROOT / "scripts" / "check_flows_clean.py", "check_flows_clean")
FLOWS = bf.build()


def test_flow_inventory_matches_doc():
    expected = {"00_common_error_handler", "01_get_token", "02_call_api", "03_call_crew", "04_emit_event", "10_verification_main", "11_step_document_fetch", "12_step_completeness", "13_step_identity",
                "14_step_authenticity", "15_step_coverage", "16_step_calculation", "20_decision_gate", "21_decision_final", "30_query_sent", "31_query_response", "40_escalation", "50_settlement",
                "60_cron_sla_and_housekeeping"}
    assert set(FLOWS) == expected


@pytest.mark.parametrize("name", sorted(FLOWS))
def test_connections_reference_existing_nodes_and_names_unique(name):
    f = FLOWS[name]
    names = [n["name"] for n in f["nodes"]]
    assert len(names) == len(set(names)), "duplicate node names"
    for src, c in f["connections"].items():
        assert src in names
        for branch in c["main"]:
            for link in branch:
                assert link["node"] in names, (src, link)
    assert any(n["type"] == "n8n-nodes-base.stickyNote" for n in f["nodes"])  # purpose note per flow


@pytest.mark.parametrize("name", sorted(FLOWS))
def test_no_wait_node_in_human_wait_flows_and_error_workflow_set(name):
    f = FLOWS[name]
    if name in ("20_decision_gate", "21_decision_final", "30_query_sent", "31_query_response", "40_escalation", "10_verification_main"):
        assert not [n for n in f["nodes"] if n["type"] == "n8n-nodes-base.wait"], "human waits must be DB rows + cron, not Wait nodes"
    if name != "00_common_error_handler":
        assert f["settings"]["errorWorkflow"] == "00_common_error_handler"
    assert f["settings"]["saveDataSuccessExecution"] == "none"


def test_webhook_flows_verify_signature_before_anything_else_and_respond_202():
    for name in ("10_verification_main", "20_decision_gate", "21_decision_final", "30_query_sent", "31_query_response", "40_escalation", "50_settlement"):
        f = FLOWS[name]
        types = {n["name"]: n for n in f["nodes"]}
        wh = next(n for n in f["nodes"] if n["type"] == "n8n-nodes-base.webhook")
        assert wh["parameters"]["options"]["rawBody"] is True and wh["parameters"]["responseMode"] == "responseNode"
        assert f["connections"][wh["name"]]["main"][0][0]["node"] == "Verify signature"
        assert f["connections"]["Verify signature"]["main"][0][0]["node"] == "IF signature ok"
        assert types["Respond 401"]["parameters"]["options"]["responseCode"] == 401 and types["Respond 202"]["parameters"]["options"]["responseCode"] == 202
        branch_true = f["connections"]["IF signature ok"]["main"][0][0]["node"]
        branch_false = f["connections"]["IF signature ok"]["main"][1][0]["node"]
        assert branch_true == "Respond 202" and branch_false == "Respond 401"


def test_signature_code_matches_api_signing_scheme():
    """The JS recomputes sha256 HMAC over the raw body with INS_N8N_WEBHOOK_SECRET - same as insurer-api orchestrator._sign."""
    js = bf.VERIFY_JS
    assert "createHmac('sha256', $env.INS_N8N_WEBHOOK_SECRET)" in js and "timingSafeEqual" in js and "replace('sha256=', '')" in js
    sys.path.insert(0, str(ROOT / "insurer" / "api"))
    from insurer_app.services import orchestrator

    body = b'{"case_id":"x"}'
    expected = "sha256=" + hmac.new(orchestrator.get_settings().n8n_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert orchestrator.sign_webhook(body) == expected


def test_main_flow_follows_step_order_and_finalizes():
    f = FLOWS["10_verification_main"]
    steps = [n["name"] for n in f["nodes"] if n["name"].startswith("Step ")]
    assert steps == ["Step document_fetch", "Step completeness", "Step identity", "Step authenticity", "Step coverage", "Step calculation"]
    assert any(n["name"] == "POST run finalize" for n in f["nodes"])
    cur = "Set run vars"
    for s in steps:
        assert f["connections"][cur]["main"][0][0]["node"] == s
        cur = s
    assert f["connections"][cur]["main"][0][0]["node"] == "POST run finalize"


def test_idempotency_key_on_state_changing_api_calls():
    for name, f in FLOWS.items():
        for n in f["nodes"]:
            p = n["parameters"]
            if n["type"] == "n8n-nodes-base.httpRequest" and p.get("method") == "POST" and p["url"].startswith("={{ $env.INS_API_URL") and "Idempotency-Key" not in json.dumps(p):
                pytest.fail(f"{name}:{n['name']} POST without Idempotency-Key")


def test_no_secrets_in_generated_flows_and_hygiene_script(tmp_path):
    for name, f in FLOWS.items():
        assert chk.check(f, name) == [], name
    d = tmp_path / "flows"
    d.mkdir()
    for name, f in FLOWS.items():
        (d / f"{name}.json").write_text(json.dumps(f), encoding="utf-8")
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_flows_clean.py"), str(d)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    bad = json.loads((d / "50_settlement.json").read_text())
    bad["nodes"][1]["credentials"] = {"oAuth2Api": {"id": "1", "name": "x", "data": {"clientSecret": "s3cr3t"}}}
    bad["nodes"][2]["parameters"] = {"url": "https://evil.example.com/hook?token=abc123", "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"}
    (d / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_flows_clean.py"), str(d)], capture_output=True, text=True)
    assert res.returncode == 1 and "credential" in res.stderr and "hard-coded host" in res.stderr and "JWT" in res.stderr and "secret in URL" in res.stderr


def test_committed_flow_files_are_up_to_date():
    """flows/*.json are generated; regenerate with `python insurer/n8n/build_flows.py` if this fails."""
    bf.main()
    for name, f in FLOWS.items():
        assert json.loads((bf.OUT / f"{name}.json").read_text(encoding="utf-8")) == f


def test_webhook_nodes_carry_a_webhook_id_and_only_trigger_flows_are_activated():
    """Both found by importing into real n8n 1.64.3: without webhookId the URL is /webhook/<wf>/<node>/<path>; sub-workflows and the error handler cannot be activated."""
    for name, f in FLOWS.items():
        for n in f["nodes"]:
            if n["type"] == "n8n-nodes-base.webhook":
                assert re.fullmatch(r"[0-9a-f-]{36}", n["webhookId"]), name
    active = (N8N / "active_flows.txt").read_text(encoding="utf-8").split()
    assert active and all(any(n["type"] in ("n8n-nodes-base.webhook", "n8n-nodes-base.cron") for n in FLOWS[a]["nodes"]) for a in active)
    assert "00_common_error_handler" not in active and all(FLOWS[n]["id"] == n for n in FLOWS)  # ids are stable so Execute Workflow references resolve
