"""Generates the insurer-n8n workflow JSON files (03-09 §3.1) so they are reviewable as code and always structurally valid.

    python insurer/n8n/build_flows.py        # writes insurer/n8n/flows/*.json

Flows hold NO business rules: they sequence calls to insurer-api / insurer-crew and follow the ``next_step`` the API returns.
Credentials are referenced by name only. Not exercised against a live n8n here - ``tests/test_flows.py`` validates structure and hygiene."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "flows"
API = "{{ $env.INS_API_URL }}"
CREW = "{{ $env.INS_CREW_URL }}"
STEPS = ["document_fetch", "completeness", "identity", "authenticity", "coverage", "calculation"]
AGENT_PATH = {"identity": "/v1/identity/analyze", "authenticity": "/v1/authenticity/analyze", "coverage": "/v1/coverage/analyze"}

VERIFY_JS = """const crypto = require('crypto');
const item = $input.first();
const raw = item.binary?.data ? Buffer.from(item.binary.data.data, 'base64') : Buffer.from(JSON.stringify(item.json.body));
const sig = (item.json.headers['x-n8n-signature'] || '').replace('sha256=', '');
const exp = crypto.createHmac('sha256', $env.INS_N8N_WEBHOOK_SECRET).update(raw).digest('hex');
const ok = sig.length === exp.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(exp));
return [{ json: { ok, body: item.json.body, event_id: item.json.headers['x-event-id'] } }];"""
STRIP_JS = "const j = $input.first().json; delete j.context; delete j.body; delete j.payload; return [{ json: j }];"


class Flow:
    def __init__(self, name: str, note: str) -> None:
        self.name = name
        self.nodes: list[dict[str, Any]] = []
        self.conns: dict[str, Any] = {}
        self._x = 0
        self.note(note)

    def note(self, text: str) -> None:
        self.nodes.append({"parameters": {"content": text, "height": 120, "width": 420}, "name": "Sticky purpose", "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [0, -220]})

    def add(self, name: str, type_: str, params: dict[str, Any], *, after: str | None = None, branch: int = 0, creds: dict[str, str] | None = None, version: int = 1) -> str:
        self._x += 240
        node: dict[str, Any] = {"parameters": params, "name": name, "type": type_, "typeVersion": version, "position": [self._x, 0 if branch == 0 else 200 * branch]}
        if creds:
            node["credentials"] = {k: {"id": "", "name": v} for k, v in creds.items()}  # name only, never values
        if type_ == "n8n-nodes-base.httpRequest":
            node["continueOnFail"] = params.pop("_continue", False)
        self.nodes.append(node)
        if after:
            self.conns.setdefault(after, {"main": [[]]})
            while len(self.conns[after]["main"]) <= branch:
                self.conns[after]["main"].append([])
            self.conns[after]["main"][branch].append({"node": name, "type": "main", "index": 0})
        return name

    def connect(self, src: str, dst: str, branch: int = 0) -> None:
        self.conns.setdefault(src, {"main": [[]]})
        while len(self.conns[src]["main"]) <= branch:
            self.conns[src]["main"].append([])
        self.conns[src]["main"][branch].append({"node": dst, "type": "main", "index": 0})

    def json(self, *, error_workflow: bool = True) -> dict[str, Any]:
        settings: dict[str, Any] = {"executionOrder": "v1", "saveDataSuccessExecution": "none", "saveDataErrorExecution": "all"}
        if error_workflow:
            settings["errorWorkflow"] = "00_common_error_handler"
        return {"id": self.name, "name": self.name, "nodes": self.nodes, "connections": self.conns, "active": False, "settings": settings, "tags": [{"id": "insurerTag00001", "name": "insurer"}]}


def http(f: Flow, name: str, method: str, url: str, body: Any = None, after: str | None = None, branch: int = 0, base: str = API, idem: bool = True, retry: bool = True) -> str:
    params: dict[str, Any] = {"method": method, "url": f"={base}{url}", "options": {"timeout": 30000 if base == API else 100000}, "sendHeaders": True,
                              "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $json.token }}"}] + ([{"name": "Idempotency-Key", "value": "={{ $crypto.createHash('sha1').update($execution.id + ':" + name + "').digest('hex') }}"}] if idem else [])}}
    if base == CREW:
        params["headerParameters"] = {"parameters": [{"name": "X-Service-Token", "value": "={{ $env.INS_CREW_SERVICE_TOKEN }}"}]}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    if retry:
        params["options"]["retry"] = {"maxTries": 3, "waitBetweenTries": 2000}
    params["_continue"] = False
    return f.add(name, "n8n-nodes-base.httpRequest", params, after=after, branch=branch, version=4)


def webhook_head(f: Flow, path: str) -> str:
    wh = f.add(f"Webhook {path}", "n8n-nodes-base.webhook", {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {"rawBody": True}}, version=2)
    # without a webhookId n8n registers /webhook/<workflowId>/<node>/<path> instead of /webhook/<path> (found by running real n8n 1.64.3)
    next(n for n in f.nodes if n["name"] == wh)["webhookId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"insurer-n8n/{f.name}/{path}"))
    ver = f.add("Verify signature", "n8n-nodes-base.function", {"functionCode": VERIFY_JS}, after=wh)
    iff = f.add("IF signature ok", "n8n-nodes-base.if", {"conditions": {"boolean": [{"value1": "={{ $json.ok }}", "value2": True}]}}, after=ver)
    f.add("Respond 401", "n8n-nodes-base.respondToWebhook", {"respondWith": "json", "responseBody": '{"code":"invalid_signature"}', "options": {"responseCode": 401}}, after=iff, branch=1)
    return f.add("Respond 202", "n8n-nodes-base.respondToWebhook", {"respondWith": "json", "responseBody": '{"accepted":true}', "options": {"responseCode": 202}}, after=iff)


def build() -> dict[str, dict[str, Any]]:
    flows: dict[str, dict[str, Any]] = {}

    f = Flow("00_common_error_handler", "Error Trigger -> strip context/body -> raise alert -> mark run failed (if known).")
    t = f.add("Error Trigger", "n8n-nodes-base.errorTrigger", {})
    s = f.add("Strip payload", "n8n-nodes-base.function", {"functionCode": STRIP_JS}, after=t)
    a = http(f, "POST alert", "POST", "/internal/alerts", '={{ JSON.stringify({severity:"high", source:"n8n", workflow:$json.workflow?.name, node:$json.execution?.lastNodeExecuted, message:$json.execution?.error?.message, execution_id:$json.execution?.id}) }}', after=s)
    f.add("Redis publish alert", "n8n-nodes-base.redis", {"operation": "publish", "channel": "alert:n8n", "messageData": "={{ JSON.stringify($json) }}"}, after=a, creds={"redis": "insurer-redis"})
    flows["00_common_error_handler"] = f.json(error_workflow=False)

    f = Flow("01_get_token", "Keycloak client-credentials token, cached in workflow static data for 4 minutes.")
    t = f.add("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", {})
    c = f.add("Check cache", "n8n-nodes-base.function", {"functionCode": "const sd = $getWorkflowStaticData('global'); const ok = sd.token && sd.exp > Date.now(); return [{json:{ok, token: sd.token}}];"}, after=t)
    i = f.add("IF cached", "n8n-nodes-base.if", {"conditions": {"boolean": [{"value1": "={{ $json.ok }}", "value2": True}]}}, after=c)
    h = f.add("POST token", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "={{ $env.INS_KEYCLOAK_TOKEN_URL }}", "sendBody": True, "contentType": "form-urlencoded", "bodyParameters": {"parameters": [{"name": "grant_type", "value": "client_credentials"}]},
                                                          "authentication": "predefinedCredentialType", "nodeCredentialType": "oAuth2Api", "options": {}}, after=i, branch=1, creds={"oAuth2Api": "insurer-keycloak-client"}, version=4)
    f.add("Store token", "n8n-nodes-base.function", {"functionCode": "const sd = $getWorkflowStaticData('global'); sd.token = $json.access_token; sd.exp = Date.now() + ($json.expires_in - 60) * 1000; return [{json:{token: sd.token}}];"}, after=h)
    flows["01_get_token"] = f.json()

    f = Flow("02_call_api", "HTTP wrapper: token, idempotency key, retries (2s/8s/30s); 409 run_in_progress is success (already=true); other 4xx are hard errors.")
    t = f.add("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", {})
    tok = f.add("Get token", "n8n-nodes-base.executeWorkflow", {"workflowId": "01_get_token"}, after=t)
    req = f.add("HTTP request", "n8n-nodes-base.httpRequest", {"method": "={{ $('Execute Workflow Trigger').first().json.method }}", "url": "={{ $env.INS_API_URL + $('Execute Workflow Trigger').first().json.path }}",
                                                               "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($('Execute Workflow Trigger').first().json.body || {}) }}", "options": {"timeout": 30000, "response": {"response": {"fullResponse": True, "neverError": True}}, "retry": {"maxTries": 3, "waitBetweenTries": 2000}}}, after=tok, version=4)
    f.add("Classify", "n8n-nodes-base.function", {"functionCode": "const r = $input.first().json; const s = r.statusCode; if (s >= 200 && s < 300) return [{json:{ok:true, status:s, body:r.body}}]; if (s === 409 && r.body?.code === 'run_in_progress') return [{json:{ok:true, already:true, status:s, body:r.body}}]; if (s >= 500) throw new Error('api_5xx_' + s); return [{json:{ok:false, hard:true, status:s, body:r.body}}];"}, after=req)
    flows["02_call_api"] = f.json()

    f = Flow("03_call_crew", "Crew wrapper: 100 s timeout, 429 busy waits Retry-After (cap 60 s, max 3), 422 returns invalid_output, 5xx returns unavailable.")
    t = f.add("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", {})
    req = f.add("HTTP request", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "={{ $env.INS_CREW_URL + $json.path }}", "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Service-Token", "value": "={{ $env.INS_CREW_SERVICE_TOKEN }}"}]},
                                                               "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}", "options": {"timeout": 100000, "response": {"response": {"fullResponse": True, "neverError": True}}}}, after=t, version=4)
    f.add("Classify crew result", "n8n-nodes-base.function", {"functionCode": "const r = $input.first().json; const s = r.statusCode; if (s === 200) return [{json:{ok:true, output:r.body}}]; if (s === 429) return [{json:{ok:false, reason:'busy', retry_after:Math.min(60, Number(r.headers['retry-after']||5))}}]; if (s === 422) return [{json:{ok:false, reason:'invalid_output'}}]; return [{json:{ok:false, reason:'unavailable'}}];"}, after=req)
    flows["03_call_crew"] = f.json()

    f = Flow("04_emit_event", "POST /internal/events {type, case_id, data}; the API republishes to the SSE stream.")
    t = f.add("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", {})
    http(f, "POST event", "POST", "/internal/events", "={{ JSON.stringify($json) }}", after=t)
    flows["04_emit_event"] = f.json()

    f = Flow("10_verification_main", "Trigger verification-start. Create run (idempotent by case_id+client_token) -> steps -> finalize -> follow API next. No business rules; the API decides skip logic via next_step.")
    prev = webhook_head(f, "verification-start")
    prev = http(f, "POST run create", "POST", "/internal/cases/{{ $('Verify signature').first().json.body.case_id }}/runs", "={{ JSON.stringify({trigger:$('Verify signature').first().json.body.trigger, steps:$('Verify signature').first().json.body.steps, client_token:$('Verify signature').first().json.body.client_token}) }}", after=prev)
    ex = f.add("IF run already exists", "n8n-nodes-base.if", {"conditions": {"boolean": [{"value1": "={{ $json.existing }}", "value2": True}]}}, after=prev)
    f.add("Exit duplicate delivery", "n8n-nodes-base.noOp", {}, after=ex)
    prev = f.add("Set run vars", "n8n-nodes-base.set", {"values": {"string": [{"name": "run_id", "value": "={{ $json.run_id }}"}, {"name": "case_id", "value": "={{ $('Verify signature').first().json.body.case_id }}"}]}}, after=ex, branch=1, version=2)
    for step in STEPS:
        sub = f"1{STEPS.index(step) + 1}_step_{step}"
        prev = f.add(f"Step {step}", "n8n-nodes-base.executeWorkflow", {"workflowId": sub, "options": {"waitForSubWorkflow": True}}, after=prev)
    fin = http(f, "POST run finalize", "POST", "/internal/runs/{{ $json.run_id }}/finalize", "={}", after=prev)
    sw = f.add("Switch on next", "n8n-nodes-base.switch", {"dataPropertyName": "next", "rules": {"rules": [{"value2": "needs_info"}, {"value2": "ready_for_decision"}, {"value2": "manual_review"}, {"value2": "failed"}]}}, after=fin, version=1)
    ev = lambda n, t_, b: f.add(n, "n8n-nodes-base.noOp", {}, after=sw, branch=b)  # noqa: E731
    ev("Query draft is created by the API (reviewer sends)", "q", 0)
    http(f, "POST event decision.ready", "POST", "/internal/events", '={{ JSON.stringify({type:"decision.ready", case_id:$json.case_id}) }}', after=sw, branch=1)
    http(f, "POST event case.manual_review", "POST", "/internal/events", '={{ JSON.stringify({type:"case.manual_review", case_id:$json.case_id}) }}', after=sw, branch=2)
    http(f, "POST alert failed run", "POST", "/internal/alerts", '={{ JSON.stringify({severity:"high", source:"n8n", message:"run failed", case_id:$json.case_id}) }}', after=sw, branch=3)
    flows["10_verification_main"] = f.json()

    for step in STEPS:
        n = STEPS.index(step) + 1
        f = Flow(f"1{n}_step_{step}", f"Step {step}: start -> (context -> crew, if applicable) -> post result; follow the API's next_step. Failure policy 6.15: crew unavailable/invalid -> post failed result, continue.")
        t = f.add("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", {})
        p = http(f, "POST step start", "POST", "/internal/runs/{{ $json.run_id }}/steps/" + step + "/start", "={}", after=t)
        if step == "document_fetch":
            p = http(f, "GET docs-status", "GET", "/internal/cases/{{ $('Execute Workflow Trigger').first().json.case_id }}/docs-status", after=p, idem=False)
            p = f.add("IF all fetched", "n8n-nodes-base.if", {"conditions": {"boolean": [{"value1": "={{ $json.all_fetched }}", "value2": True}]}}, after=p)
            f.add("Wait 10s and re-poll (max 30)", "n8n-nodes-base.wait", {"amount": 10, "unit": "seconds"}, after=p, branch=1)
            http(f, "POST step result", "POST", "/internal/runs/{{ $('Execute Workflow Trigger').first().json.run_id }}/steps/document_fetch/result", '={{ JSON.stringify({status:$json.failed?.length ? "failed" : "passed", failed:$json.failed}) }}', after=p)
        elif step == "completeness":
            http(f, "POST step result", "POST", "/internal/runs/{{ $json.run_id }}/steps/completeness/result", '={{ JSON.stringify({compute:true}) }}', after=p)
        elif step in AGENT_PATH:
            ctx = http(f, "GET context", "GET", "/internal/cases/{{ $('Execute Workflow Trigger').first().json.case_id }}/context?for=" + step, after=p, idem=False)
            crew = f.add("Call crew", "n8n-nodes-base.executeWorkflow", {"workflowId": "03_call_crew", "options": {"waitForSubWorkflow": True}}, after=ctx)
            http(f, "POST step result", "POST", "/internal/runs/{{ $('Execute Workflow Trigger').first().json.run_id }}/steps/" + step + "/result", '={{ JSON.stringify($json.ok ? {agent_output:$json.output} : {status:"failed", finding:"step.agent_" + $json.reason}) }}', after=crew)
        else:  # calculation
            cm = f.add("Map lines (crew)", "n8n-nodes-base.executeWorkflow", {"workflowId": "03_call_crew", "options": {"waitForSubWorkflow": True}}, after=p)
            calc = http(f, "POST calc", "POST", "/internal/runs/{{ $('Execute Workflow Trigger').first().json.run_id }}/calc", '={{ JSON.stringify({mapping:$json.output?.lines}) }}', after=cm)
            http(f, "POST step result", "POST", "/internal/runs/{{ $('Execute Workflow Trigger').first().json.run_id }}/steps/calculation/result", '={{ JSON.stringify({status:"passed"}) }}', after=calc)
        flows[f"1{n}_step_{step}"] = f.json()

    f = Flow("20_decision_gate", "Trigger decision-task-open: emit approval.requested (SSE toast) and mail approvers. No Wait node - SLA nudges are cron-driven.")
    p = webhook_head(f, "decision-task-open")
    http(f, "POST event approval.requested", "POST", "/internal/events", '={{ JSON.stringify({type:"approval.requested", case_id:$("Verify signature").first().json.body.case_id, data:$("Verify signature").first().json.body}) }}', after=p)
    flows["20_decision_gate"] = f.json()

    f = Flow("21_decision_final", "Trigger decision-final: ask the API to enqueue the hospital callback; approve/partial -> initiate settlement.")
    p = webhook_head(f, "decision-final")
    p = http(f, "POST callbacks decision", "POST", "/internal/cases/{{ $('Verify signature').first().json.body.case_id }}/callbacks/decision", "={}", after=p)
    sw = f.add("IF payable", "n8n-nodes-base.if", {"conditions": {"string": [{"value1": "={{ $('Verify signature').first().json.body.outcome }}", "operation": "notEqual", "value2": "reject"}]}}, after=p)
    http(f, "POST settlement initiate", "POST", "/internal/settlement/{{ $('Verify signature').first().json.body.case_id }}/initiate", "={}", after=sw)
    flows["21_decision_final"] = f.json()

    f = Flow("30_query_sent", "Trigger query-sent: emit query.sent. Reminder/timeout timers live in the cron flow (60).")
    p = webhook_head(f, "query-sent")
    http(f, "POST event query.sent", "POST", "/internal/events", '={{ JSON.stringify({type:"query.sent", case_id:$("Verify signature").first().json.body.case_id, data:$("Verify signature").first().json.body}) }}', after=p)
    flows["30_query_sent"] = f.json()

    f = Flow("31_query_response", "Trigger query-response: context -> crew triage -> API triage-result (deterministic rules decide) -> optional re-verify via own webhook.")
    p = webhook_head(f, "query-response")
    ctx = http(f, "GET context query_triage", "GET", "/internal/cases/{{ $('Verify signature').first().json.body.case_id }}/context?for=query_triage", after=p, idem=False)
    crew = f.add("Call crew triage", "n8n-nodes-base.executeWorkflow", {"workflowId": "03_call_crew", "options": {"waitForSubWorkflow": True}}, after=ctx)
    tr = http(f, "POST triage-result", "POST", "/internal/queries/{{ $('Verify signature').first().json.body.query_id }}/triage-result", '={{ JSON.stringify($json.ok ? {agent_output:$json.output} : {agent_output:null}) }}', after=crew)
    iff = f.add("IF rerun", "n8n-nodes-base.if", {"conditions": {"boolean": [{"value1": "={{ $json.rerun }}", "value2": True}]}}, after=tr)
    f.add("POST own verification-start", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "={{ $env.WEBHOOK_URL + 'webhook/verification-start' }}", "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify({case_id:$('Verify signature').first().json.body.case_id, trigger:'query_response', steps:$json.steps, client_token:$execution.id}) }}", "options": {}}, after=iff, version=4)
    flows["31_query_response"] = f.json()

    f = Flow("40_escalation", "Trigger escalation-raised: fetch the escalation pack, emit escalation.opened for senior reviewers.")
    p = webhook_head(f, "escalation-raised")
    p = http(f, "GET escalation pack", "GET", "/internal/escalations/{{ $('Verify signature').first().json.body.escalation_id }}/pack", after=p, idem=False)
    http(f, "POST event escalation.opened", "POST", "/internal/events", '={{ JSON.stringify({type:"escalation.opened", case_id:$("Verify signature").first().json.body.case_id}) }}', after=p)
    flows["40_escalation"] = f.json()

    f = Flow("50_settlement", "Trigger settlement-initiate: API calls the bank simulator; poll status (max 12 x 30 s) as a fallback to the bank callback.")
    p = webhook_head(f, "settlement-initiate")
    p = http(f, "POST settlement initiate", "POST", "/internal/settlement/{{ $('Verify signature').first().json.body.case_id }}/initiate", "={}", after=p)
    p = f.add("Wait 30s", "n8n-nodes-base.wait", {"amount": 30, "unit": "seconds"}, after=p)
    p = http(f, "GET settlement status", "GET", "/internal/settlement/{{ $('Verify signature').first().json.body.case_id }}/status", after=p, idem=False)
    f.add("IF settled or failed", "n8n-nodes-base.if", {"conditions": {"string": [{"value1": "={{ $json.status }}", "operation": "regex", "value2": "^(settled|failed)$"}]}}, after=p)
    flows["50_settlement"] = f.json()

    f = Flow("60_cron_sla_and_housekeeping", "Every 5 min: query reminders/timeouts, stuck runs, outbox kick. Nightly 02:00 IST: audit verification, config cache refresh, idempotency purge. SLA 50/80% nudges were removed by decision; round-3 escalation stays in the API.")
    t5 = f.add("Cron every 5 min", "n8n-nodes-base.cron", {"triggerTimes": {"item": [{"mode": "everyX", "value": 5, "unit": "minutes"}]}})
    due = http(f, "GET queries due", "GET", "/internal/queries/due?within=PT10M", after=t5, idem=False)
    http(f, "POST outbox kick", "POST", "/internal/outbox/kick", "={}", after=due)
    t2 = f.add("Cron nightly 02:00", "n8n-nodes-base.cron", {"triggerTimes": {"item": [{"mode": "everyDay", "hour": 2, "minute": 0}]}})
    v = http(f, "POST audit verify-all", "POST", "/internal/audit/verify-all", "={}", after=t2)
    http(f, "POST idempotency purge", "POST", "/internal/housekeeping/idempotency-purge", "={}", after=v)
    flows["60_cron_sla_and_housekeeping"] = f.json()
    return flows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    flows = build()
    for name, body in flows.items():
        (OUT / f"{name}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT.parent / "active_flows.txt").write_text("\n".join(n for n in flows if not n.startswith(("00", "01", "02", "03", "04", "11", "12", "13", "14", "15", "16"))) + "\n", encoding="utf-8")
    print(f"wrote {len(flows)} flows to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
