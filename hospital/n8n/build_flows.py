"""Generates the n8n workflow JSON (doc 08). Flows are code: edit here, run `python hospital/n8n/build_flows.py`,
commit the output; CI regenerates and fails on a diff. Node ids are uuid5 of flow+name, so re-imports update in place."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

NS = uuid.UUID("6f1c0c1e-2a7d-4c43-9d5e-0a9b1c2d3e4f")
OUT = Path(__file__).parent / "flows"
API = "$env.HOSP_API_URL"
TOKEN = "$('05 Get Token').first().json.access_token"  # noqa: S105
WH = "$('01 Webhook').first().json"
NOT_EMPTY = {"type": "boolean", "operation": "true", "singleValue": True}


class Flow:
    def __init__(self, name: str, wid: str, error_wf: str | None = "hosp_f8_error") -> None:
        self.name, self.id, self.error_wf = name, wid, error_wf
        self.nodes: list[dict[str, Any]] = []
        self.conns: dict[str, dict[str, list[list[dict[str, Any]]]]] = {}

    def add(
        self, name: str, type_: str, version: float, params: dict[str, Any], **extra: Any
    ) -> str:
        if any(n["name"] == name for n in self.nodes):
            raise ValueError(f"duplicate node name {name} in {self.name}")
        self.nodes.append(
            {
                "id": str(uuid.uuid5(NS, f"{self.id}/{name}")),
                "name": name,
                "type": type_,
                "typeVersion": version,
                "position": [260 * len(self.nodes), 0],
                "parameters": params,
                **extra,
            }
        )
        return name

    def connect(self, a: str, b: str, out: int = 0) -> None:
        outs = self.conns.setdefault(a, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": b, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:], strict=False):
            self.connect(a, b)

    # ---- node kinds -------------------------------------------------------------------------------
    def webhook(self, path: str) -> str:
        n = self.add(
            "01 Webhook",
            "n8n-nodes-base.webhook",
            2,
            {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {}},
        )
        self.nodes[-1]["webhookId"] = str(uuid.uuid5(NS, f"{self.id}/hook"))
        return n

    def cron(self, expr: str) -> str:
        return self.add(
            "01 Cron",
            "n8n-nodes-base.scheduleTrigger",
            1.2,
            {"rule": {"interval": [{"field": "cronExpression", "expression": expr}]}},
        )

    def if_(self, name: str, expr: str) -> str:
        return self.add(
            name,
            "n8n-nodes-base.if",
            2.2,
            {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                    "conditions": [
                        {
                            "id": str(uuid.uuid5(NS, f"{self.id}/{name}/c")),
                            "leftValue": "={{ " + expr + " }}",
                            "rightValue": "",
                            "operator": NOT_EMPTY,
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
        )

    def respond(self, name: str, code: int, body: dict[str, Any]) -> str:
        return self.add(
            name,
            "n8n-nodes-base.respondToWebhook",
            1.1,
            {
                "respondWith": "json",
                "responseBody": json.dumps(body),
                "options": {"responseCode": code},
            },
        )

    def noop(self, name: str) -> str:
        return self.add(name, "n8n-nodes-base.noOp", 1, {})

    def code(self, name: str, js: str, each: bool = False) -> str:
        return self.add(
            name,
            "n8n-nodes-base.code",
            2,
            {"mode": "runOnceForEachItem" if each else "runOnceForAllItems", "jsCode": js},
        )

    def wait(self, name: str, seconds: int) -> str:
        if seconds > 24 * 3600:
            raise ValueError("Wait node over 24 h")
        return self.add(
            name,
            "n8n-nodes-base.wait",
            1.1,
            {"resume": "timeInterval", "amount": seconds, "unit": "seconds"},
        )

    def execute(self, name: str, wid: str) -> str:
        return self.add(
            name,
            "n8n-nodes-base.executeWorkflow",
            1.2,
            {
                "source": "database",
                "workflowId": {"__rl": True, "value": wid, "mode": "id"},
                "options": {"waitForSubWorkflow": True},
            },
        )

    def http(
        self,
        name: str,
        method: str,
        url: str,
        body: str | None = None,
        *,
        auth: bool = True,
        retries: int = 3,
        timeout: int = 10000,
        on_error: str | None = "continueErrorOutput",
        form: dict[str, str] | None = None,
        corr: str = "$('01 Webhook').first().json.body.correlation_id || ''",
    ) -> str:
        headers = [{"name": "X-Correlation-Id", "value": "={{ " + corr + " }}"}]
        if auth:
            headers.append({"name": "Authorization", "value": "={{ 'Bearer ' + " + TOKEN + " }}"})
        p: dict[str, Any] = {
            "method": method,
            "url": "={{ " + url + " }}",
            "sendHeaders": True,
            "headerParameters": {"parameters": headers},
            "options": {"timeout": timeout},
        }
        for expr in (body, url):
            if expr and "}}" in expr:  # n8n would end the expression at the first "}}"
                raise ValueError(f"'}}}}' inside an expression in node {name}")
        if body is not None:
            p |= {
                "sendBody": True,
                "contentType": "json",
                "specifyBody": "json",
                "jsonBody": "={{ " + body + " }}",
            }
        if form is not None:
            p |= {
                "sendBody": True,
                "contentType": "form-urlencoded",
                "specifyBody": "keypair",
                "bodyParameters": {
                    "parameters": [{"name": k, "value": v} for k, v in form.items()]
                },
            }
        extra: dict[str, Any] = {
            "retryOnFail": retries > 1,
            "maxTries": retries,
            "waitBetweenTries": 2000,
        }
        if on_error:
            extra["onError"] = on_error
        return self.add(name, "n8n-nodes-base.httpRequest", 4.2, p, **extra)

    # ---- output ---------------------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.conns,
            "active": False,
            "settings": ({"errorWorkflow": self.error_wf} if self.error_wf else {})
            | {"executionOrder": "v1"},
            "pinData": {},
        }

    def write(self) -> None:
        OUT.mkdir(exist_ok=True)
        (OUT / f"{self.id}.json").write_text(
            json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"
        )


# ------------------------------------------------------------------------------------------ shared heads
def secured_webhook(f: Flow, path: str, wf_key: str, key_expr: str) -> str:
    """01 Webhook -> 02 Secret -> respond 200 -> 05 Get Token -> 06 Idempotency -> 07 Duplicate? ; returns the
    node that continues the flow on the 'not a duplicate' branch."""
    f.webhook(path)
    f.if_("02 Secret ok", "$json.headers['x-webhook-secret'] === $env.N8N_WEBHOOK_SECRET")
    f.respond("03 Accepted", 200, {"accepted": True})
    f.respond("04 Unauthorized", 401, {"code": "invalid_signature"})
    f.execute("05 Get Token", "hosp_u1_auth")
    f.add(
        "06 Idempotency",
        "n8n-nodes-base.set",
        3.4,
        {
            "mode": "manual",
            "assignments": {
                "assignments": [
                    {"id": "a1", "name": "workflow", "type": "string", "value": wf_key},
                    {
                        "id": "a2",
                        "name": "key",
                        "type": "string",
                        "value": "={{ " + key_expr + " }}",
                    },
                ]
            },
            "includeOtherFields": True,
        },
    )
    f.execute("06b Dedupe", "hosp_u2_idem")
    f.if_("07 Duplicate", "$json.duplicate === true")
    f.noop("07b Duplicate end")
    f.connect("01 Webhook", "02 Secret ok")
    f.connect("02 Secret ok", "03 Accepted", 0)
    f.connect("02 Secret ok", "04 Unauthorized", 1)
    f.chain("03 Accepted", "05 Get Token", "06 Idempotency", "06b Dedupe", "07 Duplicate")
    f.connect("07 Duplicate", "07b Duplicate end", 0)
    return "07 Duplicate"  # output 1 = continue


def with_kick(f: Flow, path: str) -> None:
    """Cron flows can also be run on demand (ops, tests): POST /webhook/<path> with the webhook secret."""
    f.webhook(path)
    f.if_("02 Secret ok", "$json.headers['x-webhook-secret'] === $env.N8N_WEBHOOK_SECRET")
    f.respond("03 Accepted", 200, {"accepted": True})
    f.respond("04 Unauthorized", 401, {"code": "invalid_signature"})
    f.connect("01 Webhook", "02 Secret ok")
    f.connect("02 Secret ok", "03 Accepted", 0)
    f.connect("02 Secret ok", "04 Unauthorized", 1)
    f.connect("03 Accepted", "05 Get Token")


def idem_header_key() -> str:
    return "$('01 Webhook').first().json.headers['x-idempotency-key'] || $('01 Webhook').first().json.body.document_id"


# ------------------------------------------------------------------------------------------ utilities
def u1() -> Flow:
    f = Flow("util-auth-token", "hosp_u1_auth", None)
    f.add(
        "00 Trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}
    )
    f.http(
        "01 Token",
        "POST",
        "$env.KEYCLOAK_TOKEN_URL",
        auth=False,
        form={
            "grant_type": "client_credentials",
            "client_id": "hospital-n8n",
            "client_secret": "={{ $env.N8N_CLIENT_SECRET }}",
        },
        on_error=None,
        corr="''",
    )
    f.code(
        "02 Shape",
        "return [{json: {access_token: $json.access_token, expires_in: $json.expires_in}}];",
    )
    f.chain("00 Trigger", "01 Token", "02 Shape")
    return f


def u2() -> Flow:
    f = Flow("util-idempotency", "hosp_u2_idem", None)
    f.add(
        "00 Trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}
    )
    f.execute("01 Get Token", "hosp_u1_auth")
    f.add(
        "02 Check",
        "n8n-nodes-base.httpRequest",
        4.2,
        {
            "method": "POST",
            "url": "={{ " + API + " + '/v1/internal/idempotency' }}",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Authorization", "value": "={{ 'Bearer ' + $json.access_token }}"}
                ]
            },
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": "={{ {workflow: $('00 Trigger').first().json.workflow, key: $('00 Trigger').first().json.key} }}",
            "options": {"timeout": 10000},
        },
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )
    f.chain("00 Trigger", "01 Get Token", "02 Check")
    return f


POLL_JS = """
const i = $input.first().json;
const base = i.base_url, id = i.job_id, every = (i.interval_s || 3) * 1000, max = (i.max_wait_s || 120) * 1000;
const headers = i.token ? {Authorization: 'Bearer ' + i.token} : {};
const t0 = Date.now();
while (Date.now() - t0 < max) {
  const r = await this.helpers.httpRequest({method: 'GET', url: `${base}/v1/jobs/${id}`, headers, json: true});
  if (r.state === 'succeeded') return [{json: {state: 'succeeded', result: r.result}}];
  if (r.state === 'failed') return [{json: {state: 'failed', error: r.error || 'job failed'}}];
  await new Promise(res => setTimeout(res, every));
}
return [{json: {state: 'timeout', error: 'timed out waiting for job'}}];
""".strip()


def u3() -> Flow:
    f = Flow("util-poll-job", "hosp_u3_poll", None)
    f.add(
        "00 Trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}
    )
    f.code("01 Poll (bounded by max_wait_s)", POLL_JS)
    f.chain("00 Trigger", "01 Poll (bounded by max_wait_s)")
    return f


# ------------------------------------------------------------------------------------------ F1
def f1() -> Flow:
    f = Flow("hosp-intake-document", "hosp_f1_intake")
    cont = secured_webhook(f, "intake/document-uploaded", "f1", idem_header_key())
    d = f"{WH}.body.document_id"
    f.http("08 Get document", "GET", f"{API} + '/v1/internal/documents/' + {d}", on_error=None)
    f.if_("09 Scan clean", "$json.scan_status === 'clean'")
    f.noop("09b Not clean: stop")
    f.http(
        "10 Vision quality",
        "POST",
        "$env.VISION_URL + '/v1/quality'",
        "{document_id: $json.id, url: $json.presigned_url}",
        auth=False,
        timeout=60000,
    )
    f.code(
        "11 Quality body",
        "const q = $json.quality_score !== undefined ? $json : {quality_score: 1, flags: []};\n"
        "return [{json: {quality_score: q.quality_score, flags: q.flags || [], has_required_stamp: q.has_required_stamp ?? null}}];",
    )
    f.http(
        "12 API quality", "POST", f"{API} + '/v1/internal/documents/' + {d} + '/quality'", "$json"
    )
    f.if_(
        "13 Blocking quality",
        "($('11 Quality body').first().json.flags || []).some(x => ['blurry','unreadable','blank','too_dark'].includes(x))",
    )
    f.http(
        "14 Mark needs review",
        "POST",
        f"{API} + '/v1/internal/documents/' + {d} + '/status'",
        "{parse_status: 'needs_review', error: 'quality_blocked'}",
    )
    f.http(
        "15 Parse (doc-pipeline)",
        "POST",
        "$env.DOCPIPE_URL + '/v1/parse'",
        f"{{document_id: {d}, case_id: {WH}.body.case_id, url: $('08 Get document').first().json.presigned_url}}",
        auth=False,
        timeout=15000,
    )
    f.add(
        "16 Poll set",
        "n8n-nodes-base.set",
        3.4,
        {
            "mode": "manual",
            "assignments": {
                "assignments": [
                    {
                        "id": "b1",
                        "name": "base_url",
                        "type": "string",
                        "value": "={{ $env.DOCPIPE_URL }}",
                    },
                    {
                        "id": "b2",
                        "name": "job_id",
                        "type": "string",
                        "value": "={{ $json.job_id }}",
                    },
                    {"id": "b3", "name": "interval_s", "type": "number", "value": 5},
                    {
                        "id": "b4",
                        "name": "max_wait_s",
                        "type": "number",
                        "value": "={{ Number($env.INTAKE_MAX_PARSE_WAIT_S || 300) }}",
                    },
                ]
            },
        },
    )
    f.execute("17 Poll job", "hosp_u3_poll")
    f.if_("18 Parse ok", "$json.state === 'succeeded'")
    f.code(
        "19 Split passes",
        "const r = $json.result || {};\nreturn (r.passes || []).map(p => ({json: {pass: p, doc_type: r.doc_type ?? null, confidence: r.doc_type_conf ?? 0}}));",
    )
    f.http(
        "20 API parse pass",
        "POST",
        f"{API} + '/v1/internal/documents/' + {d} + '/parse'",
        "$json.pass",
    )
    f.code(
        "21 Classify body",
        "const first = $('19 Split passes').first().json;\nreturn [{json: {doc_type: first.doc_type, confidence: first.confidence, source: 'auto'}}];",
    )
    f.http(
        "22 API classify", "POST", f"{API} + '/v1/internal/documents/' + {d} + '/classify'", "$json"
    )
    f.if_("23 Two passes", "($('17 Poll job').first().json.result.passes || []).length >= 2")
    f.noop("23b Done")
    f.http(
        "24 Single pass: needs review",
        "POST",
        f"{API} + '/v1/internal/documents/' + {d} + '/status'",
        "{parse_status: 'needs_review', error: 'single_pass'}",
    )
    f.http(
        "90 Mark failed",
        "POST",
        f"{API} + '/v1/internal/documents/' + {d} + '/status'",
        "{parse_status: 'failed', error: ($json.error && ($json.error.message || $json.error) || 'pipeline_failed').toString().slice(0, 400)}",
        on_error=None,
    )
    f.connect(cont, "08 Get document", 1)
    f.chain("08 Get document", "09 Scan clean")
    f.connect("09 Scan clean", "10 Vision quality", 0)
    f.connect("09 Scan clean", "09b Not clean: stop", 1)
    f.connect("10 Vision quality", "11 Quality body", 0)
    f.connect(
        "10 Vision quality", "11 Quality body", 1
    )  # vision down: carry on with default quality
    f.chain("11 Quality body", "12 API quality", "13 Blocking quality")
    f.connect("13 Blocking quality", "14 Mark needs review", 0)
    f.connect("13 Blocking quality", "15 Parse (doc-pipeline)", 1)
    f.connect("15 Parse (doc-pipeline)", "16 Poll set", 0)
    f.connect("15 Parse (doc-pipeline)", "90 Mark failed", 1)
    f.chain("16 Poll set", "17 Poll job", "18 Parse ok")
    f.connect("18 Parse ok", "19 Split passes", 0)
    f.connect("18 Parse ok", "90 Mark failed", 1)
    f.chain("19 Split passes", "20 API parse pass", "21 Classify body", "22 API classify")
    f.connect("20 API parse pass", "90 Mark failed", 1)
    f.connect("22 API classify", "90 Mark failed", 1)
    f.connect("22 API classify", "23 Two passes", 0)
    f.connect("23 Two passes", "23b Done", 0)
    f.connect("23 Two passes", "24 Single pass: needs review", 1)
    return f


# ------------------------------------------------------------------------------------------ F2..F8
def notify(f: Flow, name: str, case_expr: str, event: str, data: str = "{}") -> str:
    return f.http(
        name,
        "POST",
        f"{API} + '/v1/internal/cases/' + {case_expr} + '/notify'",
        f"{{event: '{event}', data: {data} }}",
        on_error=None,
    )


def f2() -> Flow:
    f = Flow("hosp-completeness-loop", "hosp_f2_completeness")
    cont = secured_webhook(
        f, "completeness/changed", "f2", f"{WH}.body.case_id + ':' + Math.floor(Date.now()/10000)"
    )
    notify(f, "08 Notify", f"{WH}.body.case_id", "completeness.updated")
    f.connect(cont, "08 Notify", 1)
    return f


def f3() -> Flow:
    f = Flow("hosp-claim-build", "hosp_f3_build")
    f.cron("*/5 * * * *")
    f.execute("05 Get Token", "hosp_u1_auth")
    f.http(
        "06 Return stuck builds",
        "POST",
        f"{API} + '/v1/internal/jobs/claim-build-timeouts'",
        "{}",
        corr="''",
    )
    f.chain("01 Cron", "05 Get Token", "06 Return stuck builds")
    with_kick(f, "jobs/claim-build")
    return f


def f4() -> Flow:
    f = Flow("hosp-submission-watch", "hosp_f4_submission_watch")
    cont = secured_webhook(f, "claim/submitted", "f4", idem_header_key())
    cid = f"{WH}.body.case_id"
    f.wait("08 Wait 15 min", 900)
    f.http("09 Ack status", "GET", f"{API} + '/v1/internal/cases/' + {cid} + '/ack-status'")
    f.if_("10 Acknowledged", "$json.acknowledged === true")
    f.noop("10b Done")
    notify(f, "11 Banner", cid, "submission.unacknowledged", "{minutes: 15}")
    f.wait("12 Wait to 8 h", 7 * 3600 + 45 * 60)
    f.execute("13 Get Token again", "hosp_u1_auth")
    f.add(
        "14 Ack status again",
        "n8n-nodes-base.httpRequest",
        4.2,
        {
            "method": "GET",
            "url": "={{ " + f"{API} + '/v1/internal/cases/' + {cid} + '/ack-status'" + " }}",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Authorization", "value": "={{ 'Bearer ' + $json.access_token }}"}
                ]
            },
            "options": {"timeout": 10000},
        },
    )
    f.if_("15 Acknowledged later", "$json.acknowledged === true")
    f.noop("15b Done")
    f.http(
        "16 Escalate",
        "POST",
        f"{API} + '/v1/internal/ops/events'",
        f"{{workflow: 'hosp-submission-watch', severity: 'high', error: 'claim not acknowledged after 8h', node: {cid}}}",
    )
    f.connect(cont, "08 Wait 15 min", 1)
    f.chain("08 Wait 15 min", "09 Ack status", "10 Acknowledged")
    f.connect("10 Acknowledged", "10b Done", 0)
    f.connect("10 Acknowledged", "11 Banner", 1)
    f.chain(
        "11 Banner",
        "12 Wait to 8 h",
        "13 Get Token again",
        "14 Ack status again",
        "15 Acknowledged later",
    )
    f.connect("15 Acknowledged later", "15b Done", 0)
    f.connect("15 Acknowledged later", "16 Escalate", 1)
    return f


def f5a() -> Flow:
    f = Flow("hosp-query-intake", "hosp_f5a_query_intake")
    cont = secured_webhook(f, "query/intake", "f5a", f"{WH}.body.query_id + ':intake'")
    q = f"{WH}.body.query_id"
    f.http("08 Get query", "GET", f"{API} + '/v1/internal/queries/' + {q}")
    f.http(
        "09 Crew triage",
        "POST",
        "$env.HOSP_CREW_URL + '/v1/jobs/query-triage'",
        "{query_id: $json.id, case_id: $json.case_id}",
        auth=False,
        timeout=15000,
    )
    f.add(
        "10 Poll set",
        "n8n-nodes-base.set",
        3.4,
        {
            "mode": "manual",
            "assignments": {
                "assignments": [
                    {
                        "id": "c1",
                        "name": "base_url",
                        "type": "string",
                        "value": "={{ $env.HOSP_CREW_URL }}",
                    },
                    {
                        "id": "c2",
                        "name": "job_id",
                        "type": "string",
                        "value": "={{ $json.job_id }}",
                    },
                    {"id": "c3", "name": "interval_s", "type": "number", "value": 3},
                    {"id": "c4", "name": "max_wait_s", "type": "number", "value": 60},
                ]
            },
        },
    )
    f.execute("11 Poll job", "hosp_u3_poll")
    f.if_("12 Triage ok", "$json.state === 'succeeded'")
    f.http(
        "13 Post triage",
        "POST",
        f"{API} + '/v1/internal/queries/' + {q} + '/triage-result'",
        "$json.result",
    )
    f.if_(
        "14 Auto draftable",
        "$('08 Get query').first().json.round < 3 && !$('08 Get query').first().json.escalation_risk && $('13 Post triage').first().json.triage_source === 'crew'",
    )
    f.http(
        "15 Start draft",
        "POST",
        "$env.HOSP_CREW_URL + '/v1/jobs/query-draft'",
        f"{{query_id: {q}, case_id: $('08 Get query').first().json.case_id}}",
        auth=False,
    )
    notify(
        f,
        "16 Needs owner",
        "$('08 Get query').first().json.case_id",
        "query.needs_owner",
        f"{{query_id: {q}}}",
    )
    f.connect(cont, "08 Get query", 1)
    f.chain("08 Get query", "09 Crew triage")
    f.connect("09 Crew triage", "10 Poll set", 0)
    f.connect("09 Crew triage", "16 Needs owner", 1)  # crew down: rules triage stays, owner is told
    f.chain("10 Poll set", "11 Poll job", "12 Triage ok")
    f.connect("12 Triage ok", "13 Post triage", 0)
    f.connect("12 Triage ok", "16 Needs owner", 1)
    f.chain("13 Post triage", "14 Auto draftable")
    f.connect("14 Auto draftable", "15 Start draft", 0)
    f.connect("14 Auto draftable", "16 Needs owner", 1)
    return f


def f5b() -> Flow:
    f = Flow("hosp-query-draft", "hosp_f5b_query_draft")
    cont = secured_webhook(f, "query/draft", "f5b", idem_header_key())
    q = f"{WH}.body.query_id"
    f.http(
        "08 Crew draft",
        "POST",
        "$env.HOSP_CREW_URL + '/v1/jobs/query-draft'",
        f"{{query_id: {q}, case_id: {WH}.body.case_id}}",
        auth=False,
        timeout=15000,
    )
    notify(
        f, "09 Crew unavailable", f"{WH}.body.case_id", "query.draft_failed", f"{{query_id: {q}}}"
    )
    f.connect(cont, "08 Crew draft", 1)
    f.connect("08 Crew draft", "09 Crew unavailable", 1)
    return f


def cron_job(name: str, wid: str, expr: str, path: str, kick: str) -> Flow:
    f = Flow(name, wid)
    f.cron(expr)
    f.execute("05 Get Token", "hosp_u1_auth")
    f.http("06 Run job", "POST", f"{API} + '{path}'", "{}", corr="''")
    f.chain("01 Cron", "05 Get Token", "06 Run job")
    with_kick(f, kick)
    return f


def f6() -> Flow:
    f = Flow("hosp-reminders", "hosp_f6_reminders")
    f.cron("*/5 * * * *")
    f.execute("05 Get Token", "hosp_u1_auth")
    f.http("06 Due reminders", "GET", f"{API} + '/v1/internal/reminders/due?limit=50'", corr="''")
    f.code("07 One item each", "return ($json.items || []).map(r => ({json: r}));")
    f.add("08 Batches of 10", "n8n-nodes-base.splitInBatches", 3, {"batchSize": 10, "options": {}})
    f.if_("09 In-app channel", "$json.channel === 'in_app'")
    f.http(
        "10 Fired",
        "POST",
        f"{API} + '/v1/internal/reminders/' + $json.id + '/fired'",
        "{result: 'in_app'}",
        corr="''",
    )
    f.http(
        "11 Skipped (no email in this deployment)",
        "POST",
        f"{API} + '/v1/internal/reminders/' + $json.id + '/failed'",
        "{error: 'email channel is not configured'}",
        corr="''",
    )
    f.chain("01 Cron", "05 Get Token", "06 Due reminders", "07 One item each", "08 Batches of 10")
    f.connect("08 Batches of 10", "09 In-app channel", 1)
    f.connect("09 In-app channel", "10 Fired", 0)
    f.connect("09 In-app channel", "11 Skipped (no email in this deployment)", 1)
    f.connect("10 Fired", "08 Batches of 10")
    f.connect("11 Skipped (no email in this deployment)", "08 Batches of 10")
    with_kick(f, "jobs/reminders")
    return f


def f7() -> Flow:
    f = cron_job(
        "hosp-stuck-sweeper",
        "hosp_f7_sweeper",
        "*/2 * * * *",
        "/v1/internal/jobs/sweeper",
        "jobs/sweeper",
    )
    f.http("07 Outbox stalled", "GET", f"{API} + '/v1/internal/outbox/stalled'", corr="''")
    f.if_("08 Any stalled", "($json.items || []).length > 0")
    f.http(
        "09 Tell admin",
        "POST",
        f"{API} + '/v1/internal/ops/events'",
        "{workflow: 'hosp-stuck-sweeper', severity: 'high', error: 'outbox rows are stalled or dead'}",
        corr="''",
    )
    f.chain("06 Run job", "07 Outbox stalled", "08 Any stalled")
    f.connect("08 Any stalled", "09 Tell admin", 0)
    return f


def f8() -> Flow:
    f = Flow("hosp-global-error", "hosp_f8_error", None)
    f.add("01 Error Trigger", "n8n-nodes-base.errorTrigger", 1, {})
    f.execute("05 Get Token", "hosp_u1_auth")
    f.add(
        "06 Report",
        "n8n-nodes-base.httpRequest",
        4.2,
        {
            "method": "POST",
            "url": "={{ " + API + " + '/v1/internal/ops/events' }}",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Authorization", "value": "={{ 'Bearer ' + $json.access_token }}"}
                ]
            },
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": "={{ {workflow: $('01 Error Trigger').first().json.workflow.name, execution_id: String($('01 Error Trigger').first().json.execution.id), "
            "node: $('01 Error Trigger').first().json.execution.lastNodeExecuted, error: String($('01 Error Trigger').first().json.execution.error.message).slice(0, 1500), severity: 'medium'} }}",
            "options": {"timeout": 10000},
        },
        retryOnFail=True,
        maxTries=3,
    )
    f.chain("01 Error Trigger", "05 Get Token", "06 Report")
    return f


def all_flows() -> list[Flow]:
    return [
        u1(), u2(), u3(), f1(), f2(), f3(), f4(), f5a(), f5b(),
        cron_job("hosp-query-sla-check", "hosp_f5c_query_sla", "0 * * * *", "/v1/internal/jobs/query-overdue", "jobs/query-sla"),
        f6(), f7(), f8(),
    ]  # fmt: skip


def main() -> int:
    flows = all_flows()
    for f in flows:
        f.write()
    stale = {p.name for p in OUT.glob("*.json")} - {f"{f.id}.json" for f in flows}
    for s in stale:
        (OUT / s).unlink()
    print(f"wrote {len(flows)} workflows to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
